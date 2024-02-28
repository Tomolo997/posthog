from django.conf import settings

from posthog.clickhouse.table_engines import (
    Distributed,
    ReplicationScheme,
    AggregatingMergeTree,
)

TABLE_BASE_NAME = "sessions"
SESSIONS_DATA_TABLE = lambda: f"sharded_{TABLE_BASE_NAME}"

# if updating these column definitions
# you'll need to update the explicit column definitions in the materialized view creation statement below
SESSIONS_TABLE_BASE_SQL = """
CREATE TABLE IF NOT EXISTS {table_name} ON CLUSTER '{cluster}'
(
    -- part of order by so will aggregate correctly
    session_id VARCHAR,
    -- part of order by so will aggregate correctly
    team_id Int64,
    -- ClickHouse will pick any value of distinct_id for the session
    -- this is fine since even if the distinct_id changes during a session
    -- it will still (or should still) map to the same person
    distinct_id SimpleAggregateFunction(any, String),

    min_first_timestamp SimpleAggregateFunction(min, DateTime64(6, 'UTC')),
    max_last_timestamp SimpleAggregateFunction(max, DateTime64(6, 'UTC')),

    urls SimpleAggregateFunction(groupUniqArrayArray, Array(String)),
    entry_url AggregateFunction(argMin, String, DateTime64(6, 'UTC')),
    exit_url AggregateFunction(argMax, String, DateTime64(6, 'UTC')),

    initial_referring_domain AggregateFunction(argMin, String, DateTime64(6, 'UTC')),
    initial_utm_source AggregateFunction(argMin, String, DateTime64(6, 'UTC')),
    initial_utm_campaign AggregateFunction(argMin, String, DateTime64(6, 'UTC')),
    initial_utm_medium AggregateFunction(argMin, String, DateTime64(6, 'UTC')),
    initial_utm_term AggregateFunction(argMin, String, DateTime64(6, 'UTC')),
    initial_utm_content AggregateFunction(argMin, String, DateTime64(6, 'UTC')),
    initial_gclid AggregateFunction(argMin, String, DateTime64(6, 'UTC')),
    initial_gad_source AggregateFunction(argMin, String, DateTime64(6, 'UTC')),

    -- create a map of how many times we saw each event
    event_count_map AggregateFunction(sumMap, Tuple(Array(String), Array(Int64))),
    -- duplicate the event count as a specific column for pageviews and autocaptures,
    -- as these are used in some key queries and need to be fast
    pageview_count SimpleAggregateFunction(sum, Int64),
    autocapture_count SimpleAggregateFunction(sum, Int64),
) ENGINE = {engine}
"""

SESSIONS_DATA_TABLE_ENGINE = lambda: AggregatingMergeTree(TABLE_BASE_NAME, replication_scheme=ReplicationScheme.SHARDED)

SESSIONS_TABLE_SQL = lambda: (
    SESSIONS_TABLE_BASE_SQL
    + """
    PARTITION BY toYYYYMM(min_first_timestamp)
    -- order by is used by the aggregating merge tree engine to
    -- identify candidates to merge, e.g. toDate(min_first_timestamp)
    -- would mean we would have one row per day per session_id
    -- if CH could completely merge to match the order by
    -- it is also used to organise data to make queries faster
    -- we want the fewest rows possible but also the fastest queries
    -- since we query by date and not by time
    -- and order by must be in order of increasing cardinality
    -- so we order by date first, then team_id, then session_id
    -- hopefully, this is a good balance between the two
    ORDER BY (toDate(min_first_timestamp), team_id, session_id)
SETTINGS index_granularity=512
"""
).format(
    table_name=SESSIONS_DATA_TABLE(),
    cluster=settings.CLICKHOUSE_CLUSTER,
    engine=SESSIONS_DATA_TABLE_ENGINE(),
)


def source_column(column_name: str) -> str:
    # I'm not sure what this condition should be, I'm not actually sure it's possible as materialized columns are not
    # part of migrations, so it's not possible to guarantee that a materialized column exists when a migration is run
    # if EE_AVAILABLE:
    #     return f"mat_{column_name}"
    return f"JSONExtractString(properties, '{column_name}')"


SESSIONS_TABLE_MV_SQL = (
    lambda: """
CREATE MATERIALIZED VIEW IF NOT EXISTS {table_name} ON CLUSTER '{cluster}'
TO {database}.{target_table}
AS SELECT

`$session_id` as session_id,
team_id,

-- it doesn't matter which distinct_id gets picked (it'll be somewhat random) as they can all join to the right person
any(distinct_id) as distinct_id,

min(timestamp) AS min_first_timestamp,
max(timestamp) AS max_last_timestamp,

groupUniqArray({current_url_property}) AS urls,
argMinState({current_url_property}, timestamp) as entry_url,
argMaxState({current_url_property}, timestamp) as exit_url,

argMinState({referring_domain_property}, timestamp) as initial_referring_domain,
argMinState({utm_source_property}, timestamp) as initial_utm_source,
argMinState({utm_campaign_property}, timestamp) as initial_utm_campaign,
argMinState({utm_medium_property}, timestamp) as initial_utm_medium,
argMinState({utm_term_property}, timestamp) as initial_utm_term,
argMinState({utm_content_property}, timestamp) as initial_utm_content,
argMinState({gclid_property}, timestamp) as initial_gclid,
argMinState({gad_source_property}, timestamp) as initial_gad_source,

sumMapState(([event], [toInt64(1)])) as event_count_map,
count(*) as event_count,
sumIf(1, event='$pageview') as pageview_count,
sumIf(1, event='$autocapture') as autocapture_count

FROM {database}.sharded_events
WHERE `$session_id` IS NOT NULL AND `$session_id` != ''
GROUP BY `$session_id`, team_id
""".format(
        table_name=f"{TABLE_BASE_NAME}_mv",
        target_table=f"writable_{TABLE_BASE_NAME}",
        cluster=settings.CLICKHOUSE_CLUSTER,
        database=settings.CLICKHOUSE_DATABASE,
        current_url_property=source_column("$current_url"),
        referring_domain_property=source_column("$referring_domain"),
        utm_source_property=source_column("utm_source"),
        utm_campaign_property=source_column("utm_campaign"),
        utm_medium_property=source_column("utm_medium"),
        utm_term_property=source_column("utm_term"),
        utm_content_property=source_column("utm_content"),
        gclid_property=source_column("gclid"),
        gad_source_property=source_column("gad_source"),
    )
)

# Distributed engine tables are only created if CLICKHOUSE_REPLICATED

# This table is responsible for writing to sharded_sessions based on a sharding key.
WRITABLE_SESSIONS_TABLE_SQL = lambda: SESSIONS_TABLE_BASE_SQL.format(
    table_name=f"writable_{TABLE_BASE_NAME}",
    cluster=settings.CLICKHOUSE_CLUSTER,
    engine=Distributed(
        data_table=SESSIONS_DATA_TABLE(),
        # shard via session_id so that all events for a session are on the same shard
        sharding_key="sipHash64(session_id)",
    ),
)

# This table is responsible for reading from sessions on a cluster setting
DISTRIBUTED_SESSIONS_TABLE_SQL = lambda: SESSIONS_TABLE_BASE_SQL.format(
    table_name=TABLE_BASE_NAME,
    cluster=settings.CLICKHOUSE_CLUSTER,
    engine=Distributed(
        data_table=SESSIONS_DATA_TABLE(),
        sharding_key="sipHash64(session_id)",
    ),
)
