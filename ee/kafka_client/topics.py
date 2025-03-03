from posthog.settings import TEST

suffix = "_test" if TEST else ""

KAFKA_EVENTS = f"clickhouse_events_proto{suffix}"
KAFKA_PERSON = f"clickhouse_person{suffix}"
KAFKA_PERSON_UNIQUE_ID = f"clickhouse_person_unique_id{suffix}"
KAFKA_SESSION_RECORDING_EVENTS = f"clickhouse_session_recording_events{suffix}"
KAFKA_EVENTS_PLUGIN_INGESTION = f"events_plugin_ingestion{suffix}"
KAFKA_PLUGIN_LOG_ENTRIES = f"plugin_log_entries{suffix}"
KAFKA_DEAD_LETTER_QUEUE = f"events_dead_letter_queue{suffix}"
KAFKA_GROUPS = f"clickhouse_groups{suffix}"
