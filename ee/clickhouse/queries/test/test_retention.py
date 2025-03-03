from datetime import datetime
from uuid import uuid4

import pytz

from ee.clickhouse.models.event import create_event
from ee.clickhouse.models.group import create_group
from ee.clickhouse.queries.clickhouse_retention import ClickhouseRetention
from ee.clickhouse.util import ClickhouseTestMixin, snapshot_clickhouse_queries
from posthog.models.action import Action
from posthog.models.action_step import ActionStep
from posthog.models.filters import Filter
from posthog.models.filters.retention_filter import RetentionFilter
from posthog.models.group_type_mapping import GroupTypeMapping
from posthog.models.person import Person
from posthog.queries.test.test_retention import retention_test_factory


def _create_event(**kwargs):
    kwargs.update({"event_uuid": uuid4()})
    create_event(**kwargs)


def _create_action(**kwargs):
    team = kwargs.pop("team")
    name = kwargs.pop("name")
    action = Action.objects.create(team=team, name=name)
    ActionStep.objects.create(action=action, event=name)
    return action


def _create_person(**kwargs):
    person = Person.objects.create(**kwargs)
    return person


class TestClickhouseRetention(ClickhouseTestMixin, retention_test_factory(ClickhouseRetention, _create_event, _create_person, _create_action)):  # type: ignore
    @snapshot_clickhouse_queries
    def test_groups_filtering(self):
        GroupTypeMapping.objects.create(team=self.team, group_type="organization", group_type_index=0)
        GroupTypeMapping.objects.create(team=self.team, group_type="company", group_type_index=1)

        create_group(team_id=self.team.pk, group_type_index=0, group_key="org:5", properties={"industry": "finance"})
        create_group(team_id=self.team.pk, group_type_index=0, group_key="org:6", properties={"industry": "technology"})

        Person.objects.create(team=self.team, distinct_ids=["person1", "alias1"])
        Person.objects.create(team=self.team, distinct_ids=["person2"])
        Person.objects.create(team=self.team, distinct_ids=["person3"])

        self._create_events(
            [
                ("person1", self._date(0), {"$group_0": "org:5"}),
                ("person2", self._date(0), {"$group_0": "org:6"}),
                ("person3", self._date(0)),
                ("person1", self._date(1), {"$group_0": "org:5"}),
                ("person2", self._date(1), {"$group_0": "org:6"}),
                ("person1", self._date(7), {"$group_0": "org:5"}),
                ("person2", self._date(7), {"$group_0": "org:6"}),
                ("person1", self._date(14), {"$group_0": "org:5"}),
                ("person1", self._date(month=1, day=-6), {"$group_0": "org:5"}),
                ("person2", self._date(month=1, day=-6), {"$group_0": "org:6"}),
                ("person2", self._date(month=1, day=1), {"$group_0": "org:6"}),
                ("person1", self._date(month=1, day=1), {"$group_0": "org:5"}),
                ("person2", self._date(month=1, day=15), {"$group_0": "org:6"}),
            ]
        )

        result = ClickhouseRetention().run(
            RetentionFilter(
                data={
                    "date_to": self._date(10, month=1, hour=0),
                    "period": "Week",
                    "total_intervals": 7,
                    "properties": [{"key": "industry", "value": "technology", "type": "group", "group_type_index": 0}],
                }
            ),
            self.team,
        )

        self.assertEqual(
            self.pluck(result, "values", "count"),
            [[1, 1, 0, 1, 1, 0, 1], [1, 0, 1, 1, 0, 1], [0, 0, 0, 0, 0], [1, 1, 0, 1], [1, 0, 1], [0, 0], [1],],
        )

        result = ClickhouseRetention().run(
            RetentionFilter(
                data={
                    "date_to": self._date(10, month=1, hour=0),
                    "period": "Week",
                    "total_intervals": 7,
                    "properties": [
                        {"key": "industry", "value": "", "type": "group", "group_type_index": 0, "operator": "is_set"}
                    ],
                }
            ),
            self.team,
        )

        self.assertEqual(
            self.pluck(result, "values", "count"),
            [[2, 2, 1, 2, 2, 0, 1], [2, 1, 2, 2, 0, 1], [1, 1, 1, 0, 0], [2, 2, 0, 1], [2, 0, 1], [0, 0], [1],],
        )
