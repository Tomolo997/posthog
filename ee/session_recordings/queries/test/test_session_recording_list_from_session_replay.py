from typing import Dict
from unittest import mock
from uuid import uuid4

from dateutil.relativedelta import relativedelta
from django.utils.timezone import now
from freezegun import freeze_time
from parameterized import parameterized

from ee.clickhouse.materialized_columns.columns import materialize
from posthog.clickhouse.client import sync_execute
from posthog.models import Person
from posthog.models.filters import SessionRecordingsFilter
from posthog.session_recordings.queries.session_recording_list_from_replay_summary import (
    SessionRecordingListFromReplaySummary,
)
from posthog.session_recordings.queries.test.session_replay_sql import produce_replay_summary
from posthog.session_recordings.sql.session_replay_event_sql import TRUNCATE_SESSION_REPLAY_EVENTS_TABLE_SQL
from posthog.test.base import (
    APIBaseTest,
    ClickhouseTestMixin,
    QueryMatchingTest,
    snapshot_clickhouse_queries,
    _create_event,
)
from posthog.utils import PersonOnEventsMode


@freeze_time("2021-01-01T13:46:23")
class TestClickhouseSessionRecordingsListFromSessionReplay(ClickhouseTestMixin, APIBaseTest, QueryMatchingTest):
    def tearDown(self) -> None:
        sync_execute(TRUNCATE_SESSION_REPLAY_EVENTS_TABLE_SQL())

    @property
    def base_time(self):
        return (now() - relativedelta(hours=1)).replace(microsecond=0, second=0)

    def create_event(
        self,
        distinct_id,
        timestamp,
        team=None,
        event_name="$pageview",
        properties=None,
    ):
        if team is None:
            team = self.team
        if properties is None:
            properties = {"$os": "Windows 95", "$current_url": "aloha.com/2"}
        return _create_event(
            team=team,
            event=event_name,
            timestamp=timestamp,
            distinct_id=distinct_id,
            properties=properties,
        )

    @parameterized.expand(
        [
            [
                "test_poe_v1_still_falls_back_to_person_subquery",
                True,
                False,
                False,
                PersonOnEventsMode.V1_ENABLED,
                {
                    "kperson_filter_pre__0": "rgInternal",
                    "kpersonquery_person_filter_fin__0": "rgInternal",
                    "person_uuid": None,
                    "vperson_filter_pre__0": ["false"],
                    "vpersonquery_person_filter_fin__0": ["false"],
                },
                True,
                False,
            ],
            [
                "test_poe_being_unavailable_we_fall_back_to_person_subquery",
                False,
                False,
                False,
                PersonOnEventsMode.DISABLED,
                {
                    "kperson_filter_pre__0": "rgInternal",
                    "kpersonquery_person_filter_fin__0": "rgInternal",
                    "person_uuid": None,
                    "vperson_filter_pre__0": ["false"],
                    "vpersonquery_person_filter_fin__0": ["false"],
                },
                True,
                False,
            ],
            [
                "test_allow_denormalised_props_fix_does_not_stop_all_poe_processing",
                False,
                True,
                False,
                PersonOnEventsMode.V2_ENABLED,
                {
                    "event_names": [],
                    "event_start_time": mock.ANY,
                    "event_end_time": mock.ANY,
                    "kglobal_0": "rgInternal",
                    "vglobal_0": ["false"],
                },
                False,
                True,
            ],
            [
                "test_poe_v2_available_person_properties_are_used_in_replay_listing",
                False,
                True,
                True,
                PersonOnEventsMode.V2_ENABLED,
                {
                    "event_end_time": mock.ANY,
                    "event_names": [],
                    "event_start_time": mock.ANY,
                    "kglobal_0": "rgInternal",
                    "vglobal_0": ["false"],
                },
                False,
                True,
            ],
        ]
    )
    def test_effect_of_poe_settings_on_query_generated(
        self,
        _name: str,
        poe_v1: bool,
        poe_v2: bool,
        allow_denormalized_props: bool,
        expected_poe_mode: PersonOnEventsMode,
        expected_query_params: Dict,
        unmaterialized_person_column_used: bool,
        materialized_event_column_used: bool,
    ) -> None:
        with self.settings(
            PERSON_ON_EVENTS_OVERRIDE=poe_v1,
            PERSON_ON_EVENTS_V2_OVERRIDE=poe_v2,
            ALLOW_DENORMALIZED_PROPS_IN_LISTING=allow_denormalized_props,
        ):
            assert self.team.person_on_events_mode == expected_poe_mode
            materialize("events", "rgInternal", table_column="person_properties")

            filter = SessionRecordingsFilter(
                team=self.team,
                data={
                    "properties": [
                        {
                            "key": "rgInternal",
                            "value": ["false"],
                            "operator": "exact",
                            "type": "person",
                        }
                    ]
                },
            )
            session_recording_list_instance = SessionRecordingListFromReplaySummary(filter=filter, team=self.team)
            [generated_query, query_params] = session_recording_list_instance.get_query()
            assert query_params == {
                "clamped_to_storage_ttl": mock.ANY,
                "end_time": mock.ANY,
                "limit": 51,
                "offset": 0,
                "start_time": mock.ANY,
                "team_id": self.team.id,
                **expected_query_params,
            }

            # the unmaterialized person column
            assert (
                "has(%(vperson_filter_pre__0)s, replaceRegexpAll(JSONExtractRaw(properties, %(kperson_filter_pre__0)s)"
                in generated_query
            ) is unmaterialized_person_column_used
            # materialized event column
            assert (
                'AND (  has(%(vglobal_0)s, "mat_pp_rgInternal"))' in generated_query
            ) is materialized_event_column_used
            self.assertQueryMatchesSnapshot(generated_query)

    @parameterized.expand(
        [
            ["poe and materialized columns allowed", True, True],
            ["poe and materialized columns off", True, False],
            ["poe off and materialized columns allowed", False, True],
            ["neither poe nor materialized columns", False, False],
        ]
    )
    @snapshot_clickhouse_queries
    def test_event_filter_with_person_properties_materialized(
        self, _name: str, poe2_enabled: bool, allow_denormalised_props: bool
    ) -> None:
        # KLUDGE: I couldn't figure out how to use @also_test_with_materialized_columns(person_properties=["email"])
        # KLUDGE: and the parameterized.expand decorator at the same time, so I'm manually materializing here
        materialize("events", "email", table_column="person_properties")
        materialize("person", "email")

        with self.settings(
            PERSON_ON_EVENTS_V2_OVERRIDE=poe2_enabled, ALLOW_DENORMALIZED_PROPS_IN_LISTING=allow_denormalised_props
        ):
            user_one = "test_event_filter_with_person_properties-user"
            user_two = "test_event_filter_with_person_properties-user2"
            session_id_one = f"test_event_filter_with_person_properties-1-{str(uuid4())}"
            session_id_two = f"test_event_filter_with_person_properties-2-{str(uuid4())}"

            Person.objects.create(team=self.team, distinct_ids=[user_one], properties={"email": "bla"})
            Person.objects.create(team=self.team, distinct_ids=[user_two], properties={"email": "bla2"})

            self.create_event(
                user_one,
                self.base_time,
                properties={"$session_id": session_id_one, "$window_id": str(uuid4())},
            )
            produce_replay_summary(
                distinct_id=user_one,
                session_id=session_id_one,
                first_timestamp=self.base_time,
                team_id=self.team.id,
            )
            produce_replay_summary(
                distinct_id=user_one,
                session_id=session_id_one,
                first_timestamp=(self.base_time + relativedelta(seconds=30)),
                team_id=self.team.id,
            )
            self.create_event(
                user_two,
                self.base_time,
                properties={"$session_id": session_id_two, "$window_id": str(uuid4())},
            )
            produce_replay_summary(
                distinct_id=user_two,
                session_id=session_id_two,
                first_timestamp=self.base_time,
                team_id=self.team.id,
            )
            produce_replay_summary(
                distinct_id=user_two,
                session_id=session_id_two,
                first_timestamp=(self.base_time + relativedelta(seconds=30)),
                team_id=self.team.id,
            )

            match_everyone_filter = SessionRecordingsFilter(
                team=self.team,
                data={"properties": []},
            )

            session_recording_list_instance = SessionRecordingListFromReplaySummary(
                filter=match_everyone_filter, team=self.team
            )
            (session_recordings, _) = session_recording_list_instance.run()

            assert sorted([x["session_id"] for x in session_recordings]) == sorted([session_id_one, session_id_two])

            match_bla_filter = SessionRecordingsFilter(
                team=self.team,
                data={
                    "properties": [
                        {
                            "key": "email",
                            "value": ["bla"],
                            "operator": "exact",
                            "type": "person",
                        }
                    ]
                },
            )

            session_recording_list_instance = SessionRecordingListFromReplaySummary(
                filter=match_bla_filter, team=self.team
            )
            (session_recordings, _) = session_recording_list_instance.run()

            assert len(session_recordings) == 1
            assert session_recordings[0]["session_id"] == session_id_one

    @parameterized.expand(
        [
            ["poe and materialized columns allowed", True, True],
            ["poe and materialized columns off", True, False],
            ["poe off and materialized columns allowed", False, True],
            ["neither poe nor materialized columns", False, False],
        ]
    )
    @snapshot_clickhouse_queries
    def test_event_filter_with_person_properties_not_materialized(
        self, _name: str, poe2_enabled: bool, allow_denormalised_props: bool
    ) -> None:
        # KLUDGE: I couldn't figure out how to use @also_test_with_materialized_columns(person_properties=["email"])
        # KLUDGE: and the parameterized.expand decorator at the same time,
        # KLUDGE: so I'm manually duplicating and not materializing here

        with self.settings(
            PERSON_ON_EVENTS_V2_OVERRIDE=poe2_enabled, ALLOW_DENORMALIZED_PROPS_IN_LISTING=allow_denormalised_props
        ):
            user_one = "test_event_filter_with_person_properties-user"
            user_two = "test_event_filter_with_person_properties-user2"
            session_id_one = f"test_event_filter_with_person_properties-1-{str(uuid4())}"
            session_id_two = f"test_event_filter_with_person_properties-2-{str(uuid4())}"

            Person.objects.create(team=self.team, distinct_ids=[user_one], properties={"email": "bla"})
            Person.objects.create(team=self.team, distinct_ids=[user_two], properties={"email": "bla2"})

            self.create_event(
                user_one,
                self.base_time,
                properties={"$session_id": session_id_one, "$window_id": str(uuid4())},
            )
            produce_replay_summary(
                distinct_id=user_one,
                session_id=session_id_one,
                first_timestamp=self.base_time,
                team_id=self.team.id,
            )
            produce_replay_summary(
                distinct_id=user_one,
                session_id=session_id_one,
                first_timestamp=(self.base_time + relativedelta(seconds=30)),
                team_id=self.team.id,
            )
            self.create_event(
                user_two,
                self.base_time,
                properties={"$session_id": session_id_two, "$window_id": str(uuid4())},
            )
            produce_replay_summary(
                distinct_id=user_two,
                session_id=session_id_two,
                first_timestamp=self.base_time,
                team_id=self.team.id,
            )
            produce_replay_summary(
                distinct_id=user_two,
                session_id=session_id_two,
                first_timestamp=(self.base_time + relativedelta(seconds=30)),
                team_id=self.team.id,
            )

            match_everyone_filter = SessionRecordingsFilter(
                team=self.team,
                data={"properties": []},
            )

            session_recording_list_instance = SessionRecordingListFromReplaySummary(
                filter=match_everyone_filter, team=self.team
            )
            (session_recordings, _) = session_recording_list_instance.run()

            assert sorted([x["session_id"] for x in session_recordings]) == sorted([session_id_one, session_id_two])

            match_bla_filter = SessionRecordingsFilter(
                team=self.team,
                data={
                    "properties": [
                        {
                            "key": "email",
                            "value": ["bla"],
                            "operator": "exact",
                            "type": "person",
                        }
                    ]
                },
            )

            session_recording_list_instance = SessionRecordingListFromReplaySummary(
                filter=match_bla_filter, team=self.team
            )
            (session_recordings, _) = session_recording_list_instance.run()

            assert len(session_recordings) == 1
            assert session_recordings[0]["session_id"] == session_id_one
