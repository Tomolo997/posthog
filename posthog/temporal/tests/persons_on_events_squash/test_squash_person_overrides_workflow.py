import operator
import random
import string
from collections import defaultdict
from datetime import datetime, timezone
from typing import NamedTuple, TypedDict
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from django.conf import settings
from freezegun.api import freeze_time
from temporalio.client import Client
from temporalio.testing import ActivityEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from posthog.models.person.sql import PERSON_DISTINCT_ID_OVERRIDES_TABLE_SQL
from posthog.temporal.batch_exports.squash_person_overrides import (
    QueryInputs,
    SquashPersonOverridesInputs,
    SquashPersonOverridesWorkflow,
    delete_squashed_person_overrides_from_clickhouse,
    drop_dictionary,
    optimize_person_distinct_id_overrides,
    prepare_dictionary,
    squash_events_partition,
)
from posthog.temporal.common.clickhouse import get_client


@freeze_time("2023-03-14")
@pytest.mark.parametrize(
    "inputs,expected",
    [
        (
            {"partition_ids": None, "last_n_months": 5},
            ["202303", "202302", "202301", "202212", "202211"],
        ),
        ({"last_n_months": 1}, ["202303"]),
        (
            {"partition_ids": ["202303", "202302"], "last_n_months": 3},
            ["202303", "202302"],
        ),
        (
            {"partition_ids": ["202303", "202302"], "last_n_months": None},
            ["202303", "202302"],
        ),
    ],
)
def test_workflow_inputs_yields_partition_ids(inputs, expected):
    """Assert partition keys generated by iter_partition_ids."""
    workflow_inputs = SquashPersonOverridesInputs(**inputs)
    assert list(workflow_inputs.iter_partition_ids()) == expected


@pytest.fixture
def activity_environment():
    """Return a testing temporal ActivityEnvironment."""
    return ActivityEnvironment()


@pytest_asyncio.fixture(scope="module", autouse=True)
async def ensure_database_tables(clickhouse_client, django_db_setup):
    """Ensure necessary person_distinct_id_overrides table and related exist.

    This is a module scoped fixture as most if not all tests in this module require the
    person_distinct_id_overrides table in one way or another.
    """
    await clickhouse_client.execute_query(PERSON_DISTINCT_ID_OVERRIDES_TABLE_SQL())

    yield


EVENT_TIMESTAMP = datetime.fromisoformat("2020-01-02T00:00:00.123123+00:00")
LATEST_VERSION = 8


class PersonOverrideTuple(NamedTuple):
    distinct_id: str
    person_id: UUID


@pytest_asyncio.fixture
async def person_overrides_data(clickhouse_client):
    """Produce some fake person_overrides data for testing.

    We yield a dictionary of team_id to sets of PersonOverrideTuple. These dict can be
    used to make assertions on which should be the right person id of an event.
    """
    person_overrides = {
        # These numbers are all arbitrary.
        100: {PersonOverrideTuple(str(uuid4()), uuid4()) for _ in range(5)},
        200: {PersonOverrideTuple(str(uuid4()), uuid4()) for _ in range(4)},
        300: {PersonOverrideTuple(str(uuid4()), uuid4()) for _ in range(3)},
    }

    all_test_values = []

    for team_id, person_ids in person_overrides.items():
        for distinct_id, person_id in person_ids:
            values = {
                "team_id": team_id,
                "distinct_id": distinct_id,
                "person_id": person_id,
                "version": LATEST_VERSION,
            }
            all_test_values.append(values)

    await clickhouse_client.execute_query(
        "INSERT INTO person_distinct_id_overrides FORMAT JSONEachRow", *all_test_values
    )

    yield person_overrides

    await clickhouse_client.execute_query("TRUNCATE TABLE person_distinct_id_overrides")


@pytest.fixture
def query_inputs():
    """A default set of QueryInputs to use in all tests."""
    return QueryInputs()


@pytest_asyncio.fixture
async def optimized_person_overrides(activity_environment, query_inputs):
    """Provide an optimized person_distinct_id_overrides table for testing.

    Some activities that run in unit tests depend on the person overrides table being optimized (to remove
    duplicates and older versions). So, this fixture runs the activity for those tests.
    """
    query_inputs.dry_run = False
    query_inputs.wait_for_mutations = True
    await activity_environment.run(optimize_person_distinct_id_overrides, query_inputs)

    yield


@pytest.mark.django_db
async def test_prepare_dictionary(query_inputs, activity_environment, person_overrides_data, clickhouse_client):
    """Test a DICTIONARY is created by the prepare_dictionary activity."""
    query_inputs.dictionary_name = "fancy_dictionary"
    query_inputs.dry_run = False

    await activity_environment.run(prepare_dictionary, query_inputs)

    for team_id, person_overrides in person_overrides_data.items():
        for person_override in person_overrides:
            response = await clickhouse_client.read_query(
                f"""
                SELECT
                    distinct_id,
                    dictGet(
                        '{settings.CLICKHOUSE_DATABASE}.fancy_dictionary',
                        'person_id',
                        (team_id, distinct_id)
                    ) AS person_id
                FROM (
                    SELECT
                        {team_id} AS team_id,
                        '{person_override.distinct_id}' AS distinct_id
                )
                """
            )
            ids = response.decode("utf-8").strip().split("\t")

            assert ids[0] == person_override.distinct_id
            assert UUID(ids[1]) == person_override.person_id

    await activity_environment.run(drop_dictionary, query_inputs)


@pytest_asyncio.fixture
async def older_overrides(person_overrides_data, clickhouse_client):
    """Generate extra test data that is in an older partition."""
    older_overrides = defaultdict(set)

    older_values_to_insert = []
    for team_id, person_override in person_overrides_data.items():
        for distinct_id, _ in person_override:
            older_person_id = uuid4()
            values = {
                "team_id": team_id,
                "distinct_id": distinct_id,
                "person_id": older_person_id,
                "version": LATEST_VERSION - 1,
            }

            older_overrides[team_id].add(PersonOverrideTuple(distinct_id, older_person_id))
            older_values_to_insert.append(values)

    await clickhouse_client.execute_query(
        "INSERT INTO person_distinct_id_overrides FORMAT JSONEachRow", *older_values_to_insert
    )

    yield older_overrides


@pytest_asyncio.fixture
async def newer_overrides(person_overrides_data, clickhouse_client):
    """Generate extra test data that is in a newer partition."""
    newer_overrides = defaultdict(set)

    newer_values_to_insert = []
    for team_id, person_override in person_overrides_data.items():
        for distinct_id, _ in person_override:
            newer_person_id = uuid4()
            values = {
                "team_id": team_id,
                "distinct_id": distinct_id,
                "person_id": newer_person_id,
                "version": LATEST_VERSION + 1,
            }

            newer_overrides[team_id].add(PersonOverrideTuple(distinct_id, newer_person_id))
            newer_values_to_insert.append(values)

    await clickhouse_client.execute_query(
        "INSERT INTO person_distinct_id_overrides FORMAT JSONEachRow", *newer_values_to_insert
    )

    yield newer_overrides


@pytest.mark.django_db
async def test_prepare_dictionary_with_older_overrides_present(
    query_inputs,
    activity_environment,
    person_overrides_data,
    older_overrides,
    clickhouse_client,
):
    """Test a DICTIONARY contains latest available mappings.

    Since person_distinct_id_overrides is using a ReplacingMergeTree engine, the latest version
    should be the only available in the dictionary.

    This test is a bit deceptive as optimizing the table is what takes care of older overrides,
    not the dictionary creation process.
    """
    query_inputs.dictionary_name = "fancy_dictionary"
    query_inputs.dry_run = False

    await activity_environment.run(optimize_person_distinct_id_overrides, query_inputs)
    await activity_environment.run(prepare_dictionary, query_inputs)

    for team_id, person_overrides in person_overrides_data.items():
        for person_override in person_overrides:
            response = await clickhouse_client.read_query(
                f"""
                SELECT
                    distinct_id,
                    dictGet(
                        '{settings.CLICKHOUSE_DATABASE}.fancy_dictionary',
                        'person_id',
                        (team_id, distinct_id)
                    ) AS person_id
                FROM (
                    SELECT
                        {team_id} AS team_id,
                        '{person_override.distinct_id}' AS distinct_id
                )
                """
            )
            ids = response.decode("utf-8").strip().split("\t")

            assert ids[0] == person_override.distinct_id
            assert UUID(ids[1]) == person_override.person_id

    await activity_environment.run(drop_dictionary, query_inputs)


@pytest.mark.django_db
async def test_prepare_dictionary_with_newer_overrides_after_create(
    query_inputs,
    activity_environment,
    person_overrides_data,
    clickhouse_client,
):
    """Test a dictionary contains a static set of mappings, even if new overrides arrive.

    The dictionary should be created with a LIFETIME(0) setting to avoid pulling new updates,
    and the dictionary remaining static.
    """
    query_inputs.dictionary_name = "fancy_dictionary"
    query_inputs.dry_run = False

    await activity_environment.run(prepare_dictionary, query_inputs)

    newer_values_to_insert = []
    for team_id, person_override in person_overrides_data.items():
        for distinct_id, _ in person_override:
            newer_person_id = uuid4()
            values = {
                "team_id": team_id,
                "distinct_id": distinct_id,
                "person_id": newer_person_id,
                "version": LATEST_VERSION + 1,
            }

            newer_values_to_insert.append(values)

    await clickhouse_client.execute_query(
        "INSERT INTO person_distinct_id_overrides FORMAT JSONEachRow", *newer_values_to_insert
    )

    # Ensure new updates have landed
    await clickhouse_client.execute_query("OPTIMIZE TABLE person_distinct_id_overrides FINAL")

    for team_id, person_overrides in person_overrides_data.items():
        for person_override in person_overrides:
            response = await clickhouse_client.read_query(
                f"""
                SELECT
                    distinct_id,
                    dictGet(
                        '{settings.CLICKHOUSE_DATABASE}.fancy_dictionary',
                        'person_id',
                        (team_id, distinct_id)
                    ) AS person_id
                FROM (
                    SELECT
                        {team_id} AS team_id,
                        '{person_override.distinct_id}' AS distinct_id
                )
                """
            )
            ids = response.decode("utf-8").strip().split("\t")

            assert ids[0] == person_override.distinct_id
            assert UUID(ids[1]) == person_override.person_id

    await activity_environment.run(drop_dictionary, query_inputs)


@pytest.mark.django_db
async def test_drop_dictionary(query_inputs, activity_environment, person_overrides_data, clickhouse_client):
    """Test a DICTIONARY is dropped by drop_join_table activity."""
    query_inputs.dictionary_name = "distinguished_dictionary"
    query_inputs.dry_run = False
    query_inputs.wait_for_mutations = True

    # Ensure we are starting from scratch
    await clickhouse_client.execute_query(
        f"DROP DICTIONARY IF EXISTS {settings.CLICKHOUSE_DATABASE}.{query_inputs.dictionary_name}"
    )
    response = await clickhouse_client.read_query(
        f"EXISTS DICTIONARY {settings.CLICKHOUSE_DATABASE}.{query_inputs.dictionary_name}"
    )
    before = int(response.splitlines()[0])
    assert before == 0

    await activity_environment.run(prepare_dictionary, query_inputs)

    response = await clickhouse_client.read_query(
        f"EXISTS DICTIONARY {settings.CLICKHOUSE_DATABASE}.{query_inputs.dictionary_name}"
    )
    during = int(response.splitlines()[0])
    assert during == 1

    await activity_environment.run(drop_dictionary, query_inputs)

    response = await clickhouse_client.read_query(
        f"EXISTS DICTIONARY {settings.CLICKHOUSE_DATABASE}.{query_inputs.dictionary_name}"
    )
    after = int(response.splitlines()[0])
    assert after == 0


get_team_id_old_person_id = operator.attrgetter("team_id", "old_person_id")


def is_equal_sorted(list_left, list_right, key=get_team_id_old_person_id) -> bool:
    """Compare two lists sorted by key are equal.

    Useful when we don't care about order.
    """
    return sorted(list_left, key=key) == sorted(list_right, key=key)


class EventValues(TypedDict):
    """Events to be inserted for testing."""

    uuid: UUID
    event: str
    timestamp: datetime
    person_id: str
    team_id: int


@pytest_asyncio.fixture
async def events_to_override(person_overrides_data, clickhouse_client):
    """Produce some test events for testing.

    These events will be yielded so that we can re-fetch them and assert their
    person_ids have been overriden.
    """
    all_test_events = []
    for team_id, person_ids in person_overrides_data.items():
        for distinct_id, _ in person_ids:
            values = {
                "uuid": uuid4(),
                "event": "test-event",
                "timestamp": EVENT_TIMESTAMP,
                "team_id": team_id,
                "person_id": uuid4(),
                "distinct_id": distinct_id,
            }
            all_test_events.append(values)

    await clickhouse_client.execute_query(
        "INSERT INTO sharded_events FORMAT JSONEachRow",
        *all_test_events,
    )

    yield all_test_events

    await clickhouse_client.execute_query("TRUNCATE TABLE sharded_events")


async def assert_events_have_been_overriden(overriden_events, person_overrides):
    """Assert each event in overriden_events has actually been overriden.

    We use person_overrides to assert the person_id of each event now matches the
    overriden_person_id.
    """
    async with get_client() as clickhouse_client:
        for event in overriden_events:
            response = await clickhouse_client.read_query(
                "SELECT uuid, event, team_id, distinct_id, person_id FROM events WHERE uuid = %(uuid)s",
                query_parameters={"uuid": event["uuid"]},
            )
            row = response.decode("utf-8").splitlines()[0]
            values = [value for value in row.split("\t")]
            new_event = {
                "uuid": UUID(values[0]),
                "event": values[1],
                "team_id": int(values[2]),
                "distinct_id": values[3],
                "person_id": UUID(values[4]),
            }

            assert event["uuid"] == new_event["uuid"]  # Sanity check
            assert event["team_id"] == new_event["team_id"]  # Sanity check
            assert event["event"] == new_event["event"]  # Sanity check
            assert event["person_id"] != new_event["person_id"]

            # If all is well, we should have overriden old_person_id with an override_person_id.
            # Let's find it first:
            new_person_id = [
                person_override.person_id
                for person_override in person_overrides[new_event["team_id"]]
                if person_override.distinct_id == event["distinct_id"]
            ]
            assert new_event["person_id"] == new_person_id[0]


@pytest.fixture()
def dictionary_name(request) -> str:
    try:
        return request.param
    except AttributeError:
        return "exciting_dictionary"


@pytest_asyncio.fixture
async def overrides_dictionary(optimized_person_overrides, query_inputs, activity_environment, dictionary_name):
    """Create a person overrides dictionary for testing.

    Some activities that run in unit tests depend on the overrides dictionary. We create the dictionary in
    this fixture to avoid having to copy the creation activity on every unit test that needs it. This way,
    we can keep the unit tests centered around only the activity they are testing. The tests that run the
    entire Workflow will already include these steps as part of the workflow, so the fixture is not needed.
    """
    query_inputs.dictionary_name = dictionary_name
    query_inputs.dry_run = False
    query_inputs.wait_for_mutations = True

    await activity_environment.run(prepare_dictionary, query_inputs)

    yield dictionary_name

    await activity_environment.run(drop_dictionary, query_inputs)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "dictionary_name",
    [f"squash_events_partition_dictionary_{''.join(random.choices(string.ascii_letters, k=6))}"],
    indirect=True,
)
async def test_squash_events_partition(
    overrides_dictionary,
    query_inputs,
    activity_environment,
    person_overrides_data,
    events_to_override,
    clickhouse_client,
):
    """Test events are properly squashed by running squash_events_partition.

    After running squash_events_partition, we iterate over the test events created by
    events_to_override and check the person_id associated with each of them. It should
    match the override_person_id associated with the old_person_id they used to be set to.
    """

    query_inputs.dictionary_name = overrides_dictionary
    query_inputs.partition_ids = ["202001"]
    query_inputs.dry_run = False
    query_inputs.wait_for_mutations = True

    await clickhouse_client.execute_query(
        f"SYSTEM RELOAD DICTIONARY {overrides_dictionary}",
    )

    await activity_environment.run(squash_events_partition, query_inputs)

    await assert_events_have_been_overriden(events_to_override, person_overrides_data)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "dictionary_name",
    [f"squash_events_partition_dictionary_dry_run_{''.join(random.choices(string.ascii_letters, k=6))}"],
    indirect=True,
)
async def test_squash_events_partition_dry_run(
    overrides_dictionary,
    query_inputs,
    activity_environment,
    person_overrides_data,
    events_to_override,
    clickhouse_client,
):
    """Test events are not squashed by running squash_events_partition with dry_run=True."""
    query_inputs.dictionary_name = overrides_dictionary
    query_inputs.partition_ids = ["202001"]
    query_inputs.wait_for_mutations = True
    query_inputs.dry_run = True

    await clickhouse_client.execute_query(
        f"SYSTEM RELOAD DICTIONARY {overrides_dictionary}",
    )

    await activity_environment.run(squash_events_partition, query_inputs)

    for event in events_to_override:
        response = await clickhouse_client.read_query(
            "SELECT uuid, event, team_id, person_id FROM events WHERE uuid = %(uuid)s",
            query_parameters={"uuid": event["uuid"]},
        )
        row = response.decode("utf-8").splitlines()[0]
        values = [value for value in row.split("\t")]
        new_event = {
            "uuid": UUID(values[0]),
            "event": values[1],
            "team_id": int(values[2]),
            "person_id": UUID(values[3]),
        }

        assert event["uuid"] == new_event["uuid"]  # Sanity check
        assert event["team_id"] == new_event["team_id"]  # Sanity check
        assert event["person_id"] == new_event["person_id"]  # No squash happened


@pytest.mark.django_db
@pytest.mark.parametrize(
    "dictionary_name",
    [f"squash_events_partition_dictionary_older_{''.join(random.choices(string.ascii_letters, k=6))}"],
    indirect=True,
)
async def test_squash_events_partition_with_older_overrides(
    query_inputs,
    dictionary_name,
    activity_environment,
    person_overrides_data,
    events_to_override,
    older_overrides,
):
    """Test events are properly squashed even in the prescence of older overrides.

    If we get an override from Postgres we can be sure it's the only one for a given
    old_person_id as PG constraints enforce uniqueness on the mapping. However, ClickHouse
    doesn't enforce any kind of uniqueness constraints, so our queries need to be aware there
    could be duplicate overrides present, either in the partition we are currently working
    with as well as older ones.
    """
    query_inputs.dictionary_name = dictionary_name
    query_inputs.partition_ids = ["202001"]
    query_inputs.dry_run = False
    query_inputs.wait_for_mutations = True

    await activity_environment.run(optimize_person_distinct_id_overrides, query_inputs)
    await activity_environment.run(prepare_dictionary, query_inputs)

    await activity_environment.run(squash_events_partition, query_inputs)

    await assert_events_have_been_overriden(events_to_override, person_overrides_data)

    await activity_environment.run(drop_dictionary, query_inputs)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "dictionary_name",
    [f"squash_events_partition_dictionary_newer_{''.join(random.choices(string.ascii_letters, k=6))}"],
    indirect=True,
)
async def test_squash_events_partition_with_newer_overrides(
    query_inputs,
    activity_environment,
    overrides_dictionary,
    person_overrides_data,
    events_to_override,
    newer_overrides,
    clickhouse_client,
):
    """Test events are properly squashed even in the prescence of newer overrides.

    If we get an override from Postgres we can get be sure it's the only one for a given
    old_person_id as PG constraints enforce uniqueness on the mapping. However, ClickHouse
    doesn't enforce any kind of uniqueness constraints, so our queries need to be aware there
    could be duplicate overrides present, either in the partition we are currently working
    with as well as newer ones.
    """
    query_inputs.dictionary_name = overrides_dictionary
    query_inputs.partition_ids = ["202001"]
    query_inputs.dry_run = False
    query_inputs.wait_for_mutations = True

    await clickhouse_client.execute_query(
        f"SYSTEM RELOAD DICTIONARY {overrides_dictionary}",
    )

    await activity_environment.run(squash_events_partition, query_inputs)

    await assert_events_have_been_overriden(events_to_override, newer_overrides)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "dictionary_name",
    [f"squash_events_partition_dictionary_limited_{''.join(random.choices(string.ascii_letters, k=6))}"],
    indirect=True,
)
async def test_squash_events_partition_with_limited_team_ids(
    query_inputs,
    overrides_dictionary,
    activity_environment,
    person_overrides_data,
    events_to_override,
    clickhouse_client,
):
    """Test events are properly squashed when we specify team_ids."""
    random_team = random.choice(list(person_overrides_data.keys()))
    query_inputs.dictionary_name = overrides_dictionary
    query_inputs.partition_ids = ["202001"]
    query_inputs.dry_run = False
    query_inputs.team_ids = [random_team]
    query_inputs.wait_for_mutations = True

    await clickhouse_client.execute_query(
        f"SYSTEM RELOAD DICTIONARY {overrides_dictionary}",
    )

    await activity_environment.run(squash_events_partition, query_inputs)

    with pytest.raises(AssertionError):
        # Some checks will fail as we have limited the teams overriden.
        await assert_events_have_been_overriden(events_to_override, person_overrides_data)

    # But if we only check the limited teams, there shouldn't be any issues.
    limited_events = [event for event in events_to_override if event["team_id"] == random_team]
    await assert_events_have_been_overriden(limited_events, person_overrides_data)


@pytest.mark.django_db
async def test_delete_squashed_person_overrides_from_clickhouse(
    query_inputs, activity_environment, events_to_override, person_overrides_data, clickhouse_client
):
    """Test we can delete person overrides that have already been squashed.

    For the purposes of this unit test, we take the person overrides as given. A
    comprehensive test will cover the entire worflow end-to-end.

    We insert an extra person to ensure we are not deleting persons we shouldn't
    delete.
    """
    query_inputs.partition_ids = ["202001"]
    query_inputs.dry_run = False
    query_inputs.wait_for_mutations = True

    not_overriden_distinct_id = str(uuid4())
    not_overriden_person = {
        "team_id": 1,
        "distinct_id": not_overriden_distinct_id,
        "person_id": uuid4(),
        "version": LATEST_VERSION,
    }

    await clickhouse_client.execute_query(
        "INSERT INTO person_distinct_id_overrides FORMAT JSONEachRow", not_overriden_person
    )

    await activity_environment.run(optimize_person_distinct_id_overrides, query_inputs)
    await activity_environment.run(prepare_dictionary, query_inputs)

    try:
        await activity_environment.run(delete_squashed_person_overrides_from_clickhouse, query_inputs)
    finally:
        await activity_environment.run(drop_dictionary, query_inputs)

    response = await clickhouse_client.read_query(
        "SELECT team_id, distinct_id, person_id FROM person_distinct_id_overrides"
    )
    rows = response.decode("utf-8").splitlines()

    assert len(rows) == 1

    row = rows[0].split("\t")
    assert int(row[0]) == 1
    assert row[1] == not_overriden_person["distinct_id"]
    assert UUID(row[2]) == not_overriden_person["person_id"]


@pytest.mark.django_db
async def test_delete_squashed_person_overrides_from_clickhouse_within_grace_period(
    query_inputs, activity_environment, events_to_override, person_overrides_data, clickhouse_client
):
    """Test we do not delete person overrides if they are within the grace period."""
    query_inputs.partition_ids = ["202001"]
    query_inputs.dry_run = False
    query_inputs.wait_for_mutations = True

    now = datetime.now(tz=timezone.utc)
    override_timestamp = int(now.timestamp())
    team_id, person_override = next(iter(person_overrides_data.items()))
    distinct_id, _ = next(iter(person_override))

    not_deleted_person = {
        "team_id": team_id,
        "distinct_id": distinct_id,
        "person_id": str(uuid4()),
        "version": LATEST_VERSION + 1,
        "_timestamp": override_timestamp,
    }

    await clickhouse_client.execute_query(
        "INSERT INTO person_distinct_id_overrides FORMAT JSONEachRow", not_deleted_person
    )

    await activity_environment.run(optimize_person_distinct_id_overrides, query_inputs)
    await activity_environment.run(prepare_dictionary, query_inputs)

    # Assume it will take less than 120 seconds to run the rest of the test.
    # So the row we have added should not be deleted like all the others as its _timestamp
    # was just computed from datetime.now.
    query_inputs.delete_grace_period_seconds = 120

    try:
        await activity_environment.run(delete_squashed_person_overrides_from_clickhouse, query_inputs)

    finally:
        await activity_environment.run(drop_dictionary, query_inputs)

    response = await clickhouse_client.read_query(
        "SELECT team_id, distinct_id, person_id, _timestamp FROM person_distinct_id_overrides"
    )
    rows = response.decode("utf-8").splitlines()

    assert len(rows) == 1, "Only the override within grace period should be left, but more found that were not deleted"

    row = rows[0].split("\t")
    assert int(row[0]) == not_deleted_person["team_id"]
    assert row[1] == not_deleted_person["distinct_id"]
    assert UUID(row[2]) == UUID(not_deleted_person["person_id"])
    _timestamp = datetime.strptime(row[3], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    # _timestamp is up to second precision
    assert _timestamp == now.replace(microsecond=0)


@pytest.mark.django_db
async def test_delete_squashed_person_overrides_from_clickhouse_dry_run(
    query_inputs, activity_environment, events_to_override, person_overrides_data, clickhouse_client
):
    """Test we do not delete person overrides when dry_run=True."""
    query_inputs.partition_ids = ["202001"]
    query_inputs.dry_run = True
    query_inputs.wait_for_mutations = True

    not_overriden_distinct_id = str(uuid4())
    not_overriden_person = {
        "team_id": 1,
        "distinct_id": not_overriden_distinct_id,
        "person_id": uuid4(),
        "version": LATEST_VERSION,
    }

    await clickhouse_client.execute_query(
        "INSERT INTO person_distinct_id_overrides FORMAT JSONEachRow", not_overriden_person
    )

    await activity_environment.run(delete_squashed_person_overrides_from_clickhouse, query_inputs)

    response = await clickhouse_client.read_query(
        "SELECT team_id, distinct_id, person_id FROM person_distinct_id_overrides"
    )
    rows = response.decode("utf-8").splitlines()
    expected_persons_not_deleted = sum(len(value) for value in person_overrides_data.values()) + 1

    assert len(rows) == expected_persons_not_deleted


@pytest.mark.django_db
async def test_squash_person_overrides_workflow(
    events_to_override,
    person_overrides_data,
    clickhouse_client,
):
    """Test the squash_person_overrides workflow end-to-end."""
    client = await Client.connect(
        f"{settings.TEMPORAL_HOST}:{settings.TEMPORAL_PORT}",
        namespace=settings.TEMPORAL_NAMESPACE,
    )

    workflow_id = str(uuid4())
    inputs = SquashPersonOverridesInputs(
        partition_ids=["202001"],
        dry_run=False,
    )

    async with Worker(
        client,
        task_queue=settings.TEMPORAL_TASK_QUEUE,
        workflows=[SquashPersonOverridesWorkflow],
        activities=[
            delete_squashed_person_overrides_from_clickhouse,
            drop_dictionary,
            optimize_person_distinct_id_overrides,
            prepare_dictionary,
            squash_events_partition,
        ],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        await client.execute_workflow(
            SquashPersonOverridesWorkflow.run,
            inputs,
            id=workflow_id,
            task_queue=settings.TEMPORAL_TASK_QUEUE,
        )

    await assert_events_have_been_overriden(events_to_override, person_overrides_data)

    response = await clickhouse_client.read_query("SELECT team_id, old_person_id FROM person_overrides")
    rows = response.splitlines()
    assert len(rows) == 0


@pytest.mark.django_db
async def test_squash_person_overrides_workflow_with_newer_overrides(
    events_to_override,
    person_overrides_data,
    newer_overrides,
):
    """Test the squash_person_overrides workflow end-to-end with newer overrides."""
    client = await Client.connect(
        f"{settings.TEMPORAL_HOST}:{settings.TEMPORAL_PORT}",
        namespace=settings.TEMPORAL_NAMESPACE,
    )

    workflow_id = str(uuid4())
    inputs = SquashPersonOverridesInputs(
        partition_ids=["202001"],
        dry_run=False,
    )

    async with Worker(
        client,
        task_queue=settings.TEMPORAL_TASK_QUEUE,
        workflows=[SquashPersonOverridesWorkflow],
        activities=[
            delete_squashed_person_overrides_from_clickhouse,
            drop_dictionary,
            optimize_person_distinct_id_overrides,
            prepare_dictionary,
            squash_events_partition,
        ],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        await client.execute_workflow(
            SquashPersonOverridesWorkflow.run,
            inputs,
            id=workflow_id,
            task_queue=settings.TEMPORAL_TASK_QUEUE,
        )

    await assert_events_have_been_overriden(events_to_override, newer_overrides)


@pytest.mark.django_db
async def test_squash_person_overrides_workflow_with_limited_team_ids(
    events_to_override,
    person_overrides_data,
):
    """Test the squash_person_overrides workflow end-to-end."""
    client = await Client.connect(
        f"{settings.TEMPORAL_HOST}:{settings.TEMPORAL_PORT}",
        namespace=settings.TEMPORAL_NAMESPACE,
    )

    workflow_id = str(uuid4())
    random_team = random.choice(list(person_overrides_data.keys()))
    inputs = SquashPersonOverridesInputs(
        partition_ids=["202001"],
        team_ids=[random_team],
        dry_run=False,
    )

    async with Worker(
        client,
        task_queue=settings.TEMPORAL_TASK_QUEUE,
        workflows=[SquashPersonOverridesWorkflow],
        activities=[
            delete_squashed_person_overrides_from_clickhouse,
            drop_dictionary,
            optimize_person_distinct_id_overrides,
            prepare_dictionary,
            squash_events_partition,
        ],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        await client.execute_workflow(
            SquashPersonOverridesWorkflow.run,
            inputs,
            id=workflow_id,
            task_queue=settings.TEMPORAL_TASK_QUEUE,
        )

    with pytest.raises(AssertionError):
        # Some checks will fail as we have limited the teams overriden.
        await assert_events_have_been_overriden(events_to_override, person_overrides_data)

    # But if we only check the limited teams, there shouldn't be any issues.
    limited_events = [event for event in events_to_override if event["team_id"] == random_team]
    await assert_events_have_been_overriden(limited_events, person_overrides_data)
