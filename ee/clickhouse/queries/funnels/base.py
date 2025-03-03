from abc import ABC
from typing import Any, Dict, List, Optional, Tuple, Union, cast

from django.utils import timezone
from rest_framework.exceptions import ValidationError

from ee.clickhouse.client import sync_execute
from ee.clickhouse.models.action import format_action_filter
from ee.clickhouse.models.property import get_property_string_expr, parse_prop_clauses
from ee.clickhouse.models.util import PersonPropertiesMode
from ee.clickhouse.queries.breakdown_props import format_breakdown_cohort_join_query, get_breakdown_prop_values
from ee.clickhouse.queries.funnels.funnel_event_query import FunnelEventQuery
from ee.clickhouse.sql.funnels.funnel import FUNNEL_INNER_EVENT_STEPS_QUERY
from posthog.constants import FUNNEL_WINDOW_INTERVAL, FUNNEL_WINDOW_INTERVAL_UNIT, LIMIT, TREND_FILTER_TYPE_ACTIONS
from posthog.models import Entity, Filter, Team
from posthog.queries.funnel import Funnel
from posthog.utils import relative_date_parse


class ClickhouseFunnelBase(ABC, Funnel):
    _filter: Filter
    _team: Team
    _include_timestamp: Optional[bool]
    _include_preceding_timestamp: Optional[bool]
    _no_person_limit: Optional[bool]  # used when paths are querying for filter people

    def __init__(
        self,
        filter: Filter,
        team: Team,
        include_timestamp: Optional[bool] = None,
        include_preceding_timestamp: Optional[bool] = None,
        no_person_limit: Optional[bool] = False,
    ) -> None:
        self._filter = filter
        self._team = team
        self.params = {
            "team_id": self._team.pk,
            "events": [],  # purely a speed optimization, don't need this for filtering
        }
        self._include_timestamp = include_timestamp
        self._include_preceding_timestamp = include_preceding_timestamp

        # handle default if window isn't provided
        if not self._filter.funnel_window_days and not self._filter.funnel_window_interval:
            self._filter = self._filter.with_data({FUNNEL_WINDOW_INTERVAL: 14, FUNNEL_WINDOW_INTERVAL_UNIT: "day"})

        if self._filter.funnel_window_days:
            self._filter = self._filter.with_data(
                {FUNNEL_WINDOW_INTERVAL: self._filter.funnel_window_days, FUNNEL_WINDOW_INTERVAL_UNIT: "day"}
            )

        if not self._filter.limit:
            new_limit = {LIMIT: 100}
            self._filter = self._filter.with_data(new_limit)
            self.params.update(new_limit)

        self._update_filters()

        self._no_person_limit = no_person_limit

    def run(self, *args, **kwargs):
        if len(self._filter.entities) == 0:
            return []

        results = self._exec_query()
        return self._format_results(results)

    def _update_filters(self):
        # format default dates
        data: Dict[str, Any] = {}
        if not self._filter._date_from:
            data.update({"date_from": relative_date_parse("-7d")})
        if not self._filter._date_to:
            data.update({"date_to": timezone.now()})

        if self._filter.breakdown and not self._filter.breakdown_type:
            data.update({"breakdown_type": "event"})

        for exclusion in self._filter.exclusions:
            if exclusion.funnel_from_step is None or exclusion.funnel_to_step is None:
                raise ValidationError("Exclusion event needs to define funnel steps")

            if exclusion.funnel_from_step >= exclusion.funnel_to_step:
                raise ValidationError("Exclusion event range is invalid. End of range should be greater than start.")

            if exclusion.funnel_from_step >= len(self._filter.entities) - 1:
                raise ValidationError(
                    "Exclusion event range is invalid. Start of range is greater than number of steps."
                )

            if exclusion.funnel_to_step > len(self._filter.entities) - 1:
                raise ValidationError("Exclusion event range is invalid. End of range is greater than number of steps.")

            for entity in self._filter.entities[exclusion.funnel_from_step : exclusion.funnel_to_step + 1]:
                if entity.equals(exclusion) or exclusion.is_superset(entity):
                    raise ValidationError("Exclusion event can't be the same as funnel step")

        self._filter = self._filter.with_data(data)

    def _format_single_funnel(self, results, with_breakdown=False):
        # Format of this is [step order, person count (that reached that step), array of person uuids]
        steps = []
        total_people = 0

        for step in reversed(self._filter.entities):

            if results and len(results) > 0:
                total_people += results[step.order]

            serialized_result = self._serialize_step(step, total_people, [])
            if cast(int, step.order) > 0:
                serialized_result.update(
                    {
                        "average_conversion_time": results[cast(int, step.order) + len(self._filter.entities) - 1],
                        "median_conversion_time": results[cast(int, step.order) + len(self._filter.entities) * 2 - 2],
                    }
                )
            else:
                serialized_result.update({"average_conversion_time": None, "median_conversion_time": None})

            if with_breakdown:
                serialized_result.update({"breakdown": results[-1], "breakdown_value": results[-1]})
                # important to not try and modify this value any how - as these are keys for fetching persons

            steps.append(serialized_result)

        return steps[::-1]  #  reverse

    def _format_results(self, results):
        if not results or len(results) == 0:
            return []

        if self._filter.breakdown:
            return [self._format_single_funnel(res, with_breakdown=True) for res in results]
        else:
            return self._format_single_funnel(results[0])

    def _exec_query(self) -> List[Tuple]:
        query = self.get_query()
        return sync_execute(query, self.params)

    def _get_timestamp_outer_select(self) -> str:
        if self._include_preceding_timestamp:
            return ", max_timestamp, min_timestamp"
        elif self._include_timestamp:
            return ", timestamp"
        else:
            return ""

    def _get_timestamp_selects(self) -> Tuple[str, str]:
        """
        Returns timestamp selectors for the target step and optionally the preceding step.
        In the former case, always returns the timestamp for the first and last step as well.
        """
        target_step = self._filter.funnel_step
        final_step = len(self._filter.entities) - 1
        first_step = 0

        if not target_step:
            return "", ""

        if target_step < 0:
            # the first valid dropoff argument for funnel_step is -2
            # -2 refers to persons who performed the first step but never made it to the second
            if target_step == -1:
                raise ValueError("To request dropoff of initial step use -2")

            target_step = abs(target_step) - 2
        else:
            target_step -= 1

        if self._include_preceding_timestamp:

            if target_step == 0:
                raise ValueError("Cannot request preceding step timestamp if target funnel step is the first step")

            return (
                f", latest_{target_step}, latest_{target_step - 1}",
                f", argMax(latest_{target_step}, steps) as max_timestamp, argMax(latest_{target_step - 1}, steps) as min_timestamp",
            )
        elif self._include_timestamp:
            return (
                f", latest_{target_step}, latest_{final_step}, latest_{first_step}",
                f", argMax(latest_{target_step}, steps) as timestamp, argMax(latest_{final_step}, steps) as final_timestamp, argMax(latest_{first_step}, steps) as first_timestamp",
            )
        else:
            return "", ""

    def _get_step_times(self, max_steps: int):
        conditions: List[str] = []
        for i in range(1, max_steps):
            conditions.append(
                f"if(isNotNull(latest_{i}) AND latest_{i} <= latest_{i-1} + INTERVAL {self._filter.funnel_window_interval} {self._filter.funnel_window_interval_unit_ch()}, "
                f"dateDiff('second', toDateTime(latest_{i - 1}), toDateTime(latest_{i})), NULL) step_{i}_conversion_time"
            )

        formatted = ", ".join(conditions)
        return f", {formatted}" if formatted else ""

    def _get_partition_cols(self, level_index: int, max_steps: int):
        cols: List[str] = []
        for i in range(0, max_steps):
            cols.append(f"step_{i}")
            if i < level_index:
                cols.append(f"latest_{i}")
                for exclusion_id, exclusion in enumerate(self._filter.exclusions):
                    if cast(int, exclusion.funnel_from_step) + 1 == i:
                        cols.append(f"exclusion_{exclusion_id}_latest_{exclusion.funnel_from_step}")
            else:
                duplicate_event = 0
                if i > 0 and (
                    self._filter.entities[i].equals(self._filter.entities[i - 1])
                    or self._filter.entities[i].is_superset(self._filter.entities[i - 1])
                ):
                    duplicate_event = 1
                cols.append(
                    f"min(latest_{i}) over (PARTITION by person_id {self._get_breakdown_prop()} ORDER BY timestamp DESC ROWS BETWEEN UNBOUNDED PRECEDING AND {duplicate_event} PRECEDING) latest_{i}"
                )
                for exclusion_id, exclusion in enumerate(self._filter.exclusions):
                    # exclusion starting at step i follows semantics of step i+1 in the query (since we're looking for exclusions after step i)
                    if cast(int, exclusion.funnel_from_step) + 1 == i:
                        cols.append(
                            f"min(exclusion_{exclusion_id}_latest_{exclusion.funnel_from_step}) over (PARTITION by person_id {self._get_breakdown_prop()} ORDER BY timestamp DESC ROWS BETWEEN UNBOUNDED PRECEDING AND 0 PRECEDING) exclusion_{exclusion_id}_latest_{exclusion.funnel_from_step}"
                        )
        return ", ".join(cols)

    def _get_exclusion_condition(self):
        if not self._filter.exclusions:
            return ""

        conditions = []
        for exclusion_id, exclusion in enumerate(self._filter.exclusions):
            from_time = f"latest_{exclusion.funnel_from_step}"
            to_time = f"latest_{exclusion.funnel_to_step}"
            exclusion_time = f"exclusion_{exclusion_id}_latest_{exclusion.funnel_from_step}"
            condition = (
                f"if( {exclusion_time} > {from_time} AND {exclusion_time} < "
                f"if(isNull({to_time}), {from_time} + INTERVAL {self._filter.funnel_window_interval} {self._filter.funnel_window_interval_unit_ch()}, {to_time}), 1, 0)"
            )
            conditions.append(condition)

        if conditions:
            return f", arraySum([{','.join(conditions)}]) as exclusion"
        else:
            return ""

    def _get_sorting_condition(self, curr_index: int, max_steps: int):

        if curr_index == 1:
            return "1"

        conditions: List[str] = []
        for i in range(1, curr_index):
            conditions.append(f"latest_{i - 1} < latest_{i }")
            conditions.append(
                f"latest_{i} <= latest_0 + INTERVAL {self._filter.funnel_window_interval} {self._filter.funnel_window_interval_unit_ch()}"
            )

        return f"if({' AND '.join(conditions)}, {curr_index}, {self._get_sorting_condition(curr_index - 1, max_steps)})"

    def _get_inner_event_query(
        self, entities=None, entity_name="events", skip_entity_filter=False, skip_step_filter=False
    ) -> str:
        entities_to_use = entities or self._filter.entities

        event_query, params = FunnelEventQuery(filter=self._filter, team_id=self._team.pk).get_query(
            entities_to_use, entity_name, skip_entity_filter=skip_entity_filter
        )

        self.params.update(params)

        if skip_step_filter:
            steps_conditions = "1=1"
        else:
            steps_conditions = self._get_steps_conditions(length=len(entities_to_use))

        all_step_cols: List[str] = []
        for index, entity in enumerate(entities_to_use):
            step_cols = self._get_step_col(entity, index, entity_name)
            all_step_cols.extend(step_cols)

        for exclusion_id, entity in enumerate(self._filter.exclusions):
            step_cols = self._get_step_col(entity, entity.funnel_from_step, entity_name, f"exclusion_{exclusion_id}_")
            # every exclusion entity has the form: exclusion_<id>_step_i & timestamp exclusion_<id>_latest_i
            # where i is the starting step for exclusion on that entity
            all_step_cols.extend(step_cols)

        steps = ", ".join(all_step_cols)

        select_prop = self._get_breakdown_select_prop()
        breakdown_conditions = ""
        extra_conditions = ""
        extra_join = ""

        if self._filter.breakdown:
            if self._filter.breakdown_type == "cohort":
                extra_join = self._get_cohort_breakdown_join()
            else:
                breakdown_conditions = self._get_breakdown_conditions()
                extra_conditions = f" AND {breakdown_conditions}" if breakdown_conditions and select_prop else ""

        return FUNNEL_INNER_EVENT_STEPS_QUERY.format(
            steps=steps,
            event_query=event_query,
            extra_join=extra_join,
            steps_condition=steps_conditions,
            select_prop=select_prop,
            extra_conditions=extra_conditions,
        )

    def _get_steps_conditions(self, length: int) -> str:
        step_conditions: List[str] = []

        for index in range(length):
            step_conditions.append(f"step_{index} = 1")

        for exclusion_id, entity in enumerate(self._filter.exclusions):
            step_conditions.append(f"exclusion_{exclusion_id}_step_{entity.funnel_from_step} = 1")

        return " OR ".join(step_conditions)

    def _get_step_col(self, entity: Entity, index: int, entity_name: str, step_prefix: str = "") -> List[str]:
        # step prefix is used to distinguish actual steps, and exclusion steps
        # without the prefix, we get the same parameter binding for both, which borks things up
        step_cols: List[str] = []
        condition = self._build_step_query(entity, index, entity_name, step_prefix)
        step_cols.append(f"if({condition}, 1, 0) as {step_prefix}step_{index}")
        step_cols.append(f"if({step_prefix}step_{index} = 1, timestamp, null) as {step_prefix}latest_{index}")

        return step_cols

    def _build_step_query(self, entity: Entity, index: int, entity_name: str, step_prefix: str) -> str:
        filters = self._build_filters(entity, index)
        if entity.type == TREND_FILTER_TYPE_ACTIONS:
            action = entity.get_action()
            for action_step in action.steps.all():
                if entity_name not in self.params[entity_name]:
                    self.params[entity_name].append(action_step.event)
            action_query, action_params = format_action_filter(action, f"{entity_name}_{step_prefix}step_{index}")
            if action_query == "":
                return ""

            self.params.update(action_params)
            content_sql = "{actions_query} {filters}".format(actions_query=action_query, filters=filters,)
        else:
            if entity.id not in self.params[entity_name]:
                self.params[entity_name].append(entity.id)
            event_param_key = f"{entity_name}_{step_prefix}event_{index}"
            self.params[event_param_key] = entity.id
            content_sql = f"event = %({event_param_key})s {filters}"
        return content_sql

    def _build_filters(self, entity: Entity, index: int) -> str:
        prop_filters, prop_filter_params = parse_prop_clauses(
            entity.properties,
            self._team.pk,
            prepend=str(index),
            person_properties_mode=PersonPropertiesMode.USING_PERSON_PROPERTIES_COLUMN,
        )
        self.params.update(prop_filter_params)
        if entity.properties:
            return prop_filters
        return ""

    def _get_funnel_person_step_condition(self):
        step_num = self._filter.funnel_step
        custom_steps = self._filter.funnel_custom_steps
        max_steps = len(self._filter.entities)

        conditions = []

        if custom_steps:
            self.params.update({"custom_step_num": custom_steps})
            conditions.append("steps IN %(custom_step_num)s")
        elif step_num is not None:
            if step_num >= 0:
                self.params.update({"step_num": [i for i in range(step_num, max_steps + 1)]})
                conditions.append("steps IN %(step_num)s")
            else:
                self.params.update({"step_num": abs(step_num) - 1})
                conditions.append("steps = %(step_num)s")
        else:
            raise ValueError("Missing both funnel_step and funnel_custom_steps")

        if self._filter.funnel_step_breakdown is not None:
            prop_vals = self._parse_breakdown_prop_value()
            self.params.update({"breakdown_prop_value": prop_vals})
            conditions.append("prop IN %(breakdown_prop_value)s")

        return " AND ".join(conditions)

    def _parse_breakdown_prop_value(self):
        prop_vals: List[Union[str, int]] = (
            [val.strip() for val in self._filter.funnel_step_breakdown.split(",")]
            if isinstance(self._filter.funnel_step_breakdown, str)
            else [cast(int, self._filter.funnel_step_breakdown)]
        )
        return prop_vals

    def _get_count_columns(self, max_steps: int):
        cols: List[str] = []

        for i in range(max_steps):
            cols.append(f"countIf(steps = {i + 1}) step_{i + 1}")

        return ", ".join(cols)

    def _get_step_time_names(self, max_steps: int):
        names = []
        for i in range(1, max_steps):
            names.append(f"step_{i}_conversion_time")

        formatted = ",".join(names)
        return f", {formatted}" if formatted else ""

    def _get_step_time_avgs(self, max_steps: int, inner_query: bool = False):
        conditions: List[str] = []
        for i in range(1, max_steps):
            conditions.append(
                f"avg(step_{i}_conversion_time) step_{i}_average_conversion_time_inner"
                if inner_query
                else f"avg(step_{i}_average_conversion_time_inner) step_{i}_average_conversion_time"
            )

        formatted = ", ".join(conditions)
        return f", {formatted}" if formatted else ""

    def _get_step_time_median(self, max_steps: int, inner_query: bool = False):
        conditions: List[str] = []
        for i in range(1, max_steps):
            conditions.append(
                f"median(step_{i}_conversion_time) step_{i}_median_conversion_time_inner"
                if inner_query
                else f"median(step_{i}_median_conversion_time_inner) step_{i}_median_conversion_time"
            )

        formatted = ", ".join(conditions)
        return f", {formatted}" if formatted else ""

    def get_query(self) -> str:
        raise NotImplementedError()

    def get_step_counts_query(self) -> str:
        raise NotImplementedError()

    def get_step_counts_without_aggregation_query(self) -> str:
        raise NotImplementedError()

    def _get_breakdown_select_prop(self) -> str:
        if self._filter.breakdown:
            self.params.update({"breakdown": self._filter.breakdown})
            if self._filter.breakdown_type == "person":
                # :TRICKY: We only support string breakdown for event/person properties
                assert isinstance(self._filter.breakdown, str)
                expression, _ = get_property_string_expr(
                    "person", self._filter.breakdown, "%(breakdown)s", "person_props"
                )
                return f", {expression} AS prop"
            elif self._filter.breakdown_type == "event":
                # :TRICKY: We only support string breakdown for event/person properties
                assert isinstance(self._filter.breakdown, str)
                expression, _ = get_property_string_expr(
                    "events", self._filter.breakdown, "%(breakdown)s", "properties"
                )
                return f", {expression} AS prop"
            elif self._filter.breakdown_type == "cohort":
                return ", value AS prop"

        return ""

    def _get_cohort_breakdown_join(self) -> str:
        cohort_queries, ids, cohort_params = format_breakdown_cohort_join_query(self._team.pk, self._filter)
        self.params.update({"breakdown_values": ids})
        self.params.update(cohort_params)
        return f"""
            INNER JOIN (
                {cohort_queries}
            ) cohort_join
            ON events.distinct_id = cohort_join.distinct_id
        """

    def _get_breakdown_conditions(self) -> str:
        if self._filter.breakdown:
            limit = self._filter.breakdown_limit_or_default
            first_entity = self._filter.entities[0]

            values = get_breakdown_prop_values(
                self._filter, first_entity, "count(*)", self._team.pk, limit, extra_params={"offset": 0}
            )
            # For people, pagination sets the offset param, which is common across filters
            # and gives us the wrong breakdown values here, so we override it.
            # For events, we assume breakdown values remain stable across the funnel,
            # so using just the first entity to get breakdown values is ok.

            self.params.update({"breakdown_values": values})

        return ""

    def _get_breakdown_prop(self, group_remaining=False) -> str:
        if self._filter.breakdown:
            if group_remaining and self._filter.breakdown_type != "cohort":
                return ", if(has(%(breakdown_values)s, prop), prop, 'Other') as prop"
            else:
                return ", prop"
        else:
            return ""
