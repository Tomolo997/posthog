import json
import uuid
from typing import List, cast, Dict, Optional, Any
from unittest import mock
from unittest.mock import MagicMock, call, patch, ANY

from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.http import HttpResponse
from freezegun import freeze_time
from parameterized import parameterized
from rest_framework import status
from temporalio.service import RPCError

from posthog.api.test.batch_exports.conftest import start_test_worker
from posthog.temporal.common.schedule import describe_schedule
from posthog.constants import AvailableFeature
from posthog.models import EarlyAccessFeature, ActivityLog
from posthog.models.async_deletion.async_deletion import AsyncDeletion, DeletionType
from posthog.models.dashboard import Dashboard
from posthog.models.instance_setting import get_instance_setting
from posthog.models.organization import Organization, OrganizationMembership
from posthog.models.team import Team
from posthog.models.team.team import get_team_in_cache
from posthog.temporal.common.client import sync_connect
from posthog.test.base import APIBaseTest


class TestTeamAPI(APIBaseTest):
    def _assert_activity_log(self, expected: List[Dict], team_id: Optional[int] = None) -> None:
        if not team_id:
            team_id = self.team.pk

        starting_log_response = self.client.get(f"/api/projects/{team_id}/activity")
        assert starting_log_response.status_code == 200
        assert starting_log_response.json()["results"] == expected

    def _assert_organization_activity_log(self, expected: List[Dict]) -> None:
        starting_log_response = self.client.get(f"/api/organizations/{self.organization.pk}/activity")
        assert starting_log_response.status_code == 200
        assert starting_log_response.json()["results"] == expected

    def _assert_activity_log_is_empty(self) -> None:
        self._assert_activity_log([])

    def test_list_projects(self):
        response = self.client.get("/api/projects/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Listing endpoint always uses the simplified serializer
        response_data = response.json()
        self.assertEqual(len(response_data["results"]), 1)
        self.assertEqual(response_data["results"][0]["name"], self.team.name)
        self.assertNotIn("test_account_filters", response_data["results"][0])
        self.assertNotIn("data_attributes", response_data["results"][0])

        # TODO: These assertions will no longer make sense when we fully remove these attributes from the model
        self.assertNotIn("event_names", response_data["results"][0])
        self.assertNotIn("event_properties", response_data["results"][0])
        self.assertNotIn("event_properties_numerical", response_data["results"][0])

    def test_retrieve_project(self):
        response = self.client.get("/api/projects/@current/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response_data = response.json()
        self.assertEqual(response_data["name"], self.team.name)
        self.assertEqual(response_data["timezone"], "UTC")
        self.assertEqual(response_data["is_demo"], False)
        self.assertEqual(response_data["slack_incoming_webhook"], self.team.slack_incoming_webhook)
        self.assertEqual(response_data["has_group_types"], False)
        self.assertEqual(
            response_data["person_on_events_querying_enabled"],
            get_instance_setting("PERSON_ON_EVENTS_ENABLED") or get_instance_setting("PERSON_ON_EVENTS_V2_ENABLED"),
        )
        self.assertEqual(
            response_data["groups_on_events_querying_enabled"],
            get_instance_setting("GROUPS_ON_EVENTS_ENABLED"),
        )

        # TODO: These assertions will no longer make sense when we fully remove these attributes from the model
        self.assertNotIn("event_names", response_data)
        self.assertNotIn("event_properties", response_data)
        self.assertNotIn("event_properties_numerical", response_data)
        self.assertNotIn("event_names_with_usage", response_data)
        self.assertNotIn("event_properties_with_usage", response_data)

    def test_cant_retrieve_project_from_another_org(self):
        org = Organization.objects.create(name="New Org")
        team = Team.objects.create(organization=org, name="Default Project")

        response = self.client.get(f"/api/projects/{team.pk}/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.json(), self.not_found_response())

    @patch("posthog.api.team.get_geoip_properties")
    def test_ip_location_is_used_for_new_project_week_day_start(self, get_geoip_properties_mock: MagicMock):
        self.organization.available_features = cast(List[str], [AvailableFeature.ORGANIZATIONS_PROJECTS])
        self.organization.save()
        self.organization_membership.level = OrganizationMembership.Level.ADMIN
        self.organization_membership.save()

        get_geoip_properties_mock.return_value = {}
        response = self.client.post("/api/projects/", {"name": "Test World"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.json())
        self.assertDictContainsSubset({"name": "Test World", "week_start_day": None}, response.json())

        get_geoip_properties_mock.return_value = {"$geoip_country_code": "US"}
        response = self.client.post("/api/projects/", {"name": "Test US"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.json())
        self.assertDictContainsSubset({"name": "Test US", "week_start_day": 0}, response.json())

        get_geoip_properties_mock.return_value = {"$geoip_country_code": "PL"}
        response = self.client.post("/api/projects/", {"name": "Test PL"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.json())
        self.assertDictContainsSubset({"name": "Test PL", "week_start_day": 1}, response.json())

        get_geoip_properties_mock.return_value = {"$geoip_country_code": "IR"}
        response = self.client.post("/api/projects/", {"name": "Test IR"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.json())
        self.assertDictContainsSubset({"name": "Test IR", "week_start_day": 0}, response.json())

    def test_cant_create_team_without_license_on_selfhosted(self):
        with self.is_cloud(False):
            response = self.client.post("/api/projects/", {"name": "Test"})
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
            self.assertEqual(Team.objects.count(), 1)
            response = self.client.post("/api/projects/", {"name": "Test"})
            self.assertEqual(Team.objects.count(), 1)

    def test_cant_create_a_second_project_without_license(self):
        self.organization_membership.level = OrganizationMembership.Level.ADMIN
        self.organization_membership.save()
        response = self.client.post("/api/projects/", {"name": "Hedgebox", "is_demo": False})

        self.assertEqual(Team.objects.count(), 1)
        self.assertEqual(response.status_code, 403)
        response_data = response.json()
        self.assertDictContainsSubset(
            {
                "type": "authentication_error",
                "code": "permission_denied",
                "detail": "You must upgrade your PostHog plan to be able to create and manage multiple projects.",
            },
            response_data,
        )

        # another request without the is_demo parameter
        response = self.client.post("/api/projects/", {"name": "Hedgebox"})
        self.assertEqual(Team.objects.count(), 1)
        self.assertEqual(response.status_code, 403)
        response_data = response.json()
        self.assertDictContainsSubset(
            {
                "type": "authentication_error",
                "code": "permission_denied",
                "detail": "You must upgrade your PostHog plan to be able to create and manage multiple projects.",
            },
            response_data,
        )

    @freeze_time("2022-02-08")
    def test_update_project_timezone(self):
        self._assert_activity_log_is_empty()

        response = self.client.patch("/api/projects/@current/", {"timezone": "Europe/Lisbon"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response_data = response.json()
        self.assertEqual(response_data["name"], self.team.name)
        self.assertEqual(response_data["timezone"], "Europe/Lisbon")

        self.team.refresh_from_db()
        self.assertEqual(self.team.timezone, "Europe/Lisbon")

        self._assert_activity_log(
            [
                {
                    "activity": "updated",
                    "created_at": "2022-02-08T00:00:00Z",
                    "detail": {
                        "changes": [
                            {
                                "action": "changed",
                                "after": "Europe/Lisbon",
                                "before": "UTC",
                                "field": "timezone",
                                "type": "Team",
                            },
                        ],
                        "name": "Default Project",
                        "short_id": None,
                        "trigger": None,
                        "type": None,
                    },
                    "item_id": str(self.team.pk),
                    "scope": "Team",
                    "user": {
                        "email": "user1@posthog.com",
                        "first_name": "",
                    },
                },
            ]
        )

    def test_update_test_filter_default_checked(self):
        response = self.client.patch("/api/projects/@current/", {"test_account_filters_default_checked": "true"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response_data = response.json()
        self.assertEqual(response_data["test_account_filters_default_checked"], True)

        self.team.refresh_from_db()
        self.assertEqual(self.team.test_account_filters_default_checked, True)

    def test_cannot_set_invalid_timezone_for_project(self):
        response = self.client.patch("/api/projects/@current/", {"timezone": "America/I_Dont_Exist"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.json(),
            {
                "type": "validation_error",
                "code": "invalid_choice",
                "detail": '"America/I_Dont_Exist" is not a valid choice.',
                "attr": "timezone",
            },
        )

        self.team.refresh_from_db()
        self.assertNotEqual(self.team.timezone, "America/I_Dont_Exist")

    def test_cant_update_project_from_another_org(self):
        org = Organization.objects.create(name="New Org")
        team = Team.objects.create(organization=org, name="Default Project")

        response = self.client.patch(f"/api/projects/{team.pk}/", {"timezone": "Africa/Accra"})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.json(), self.not_found_response())

        team.refresh_from_db()
        self.assertEqual(team.timezone, "UTC")

    def test_filter_permission(self):
        response = self.client.patch(
            f"/api/projects/{self.team.id}/",
            {"test_account_filters": [{"key": "$current_url", "value": "test"}]},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response_data = response.json()
        self.assertEqual(response_data["name"], self.team.name)
        self.assertEqual(
            response_data["test_account_filters"],
            [{"key": "$current_url", "value": "test"}],
        )

    @freeze_time("2022-02-08")
    @patch("posthog.api.team.delete_bulky_postgres_data")
    @patch("posthoganalytics.capture")
    def test_delete_team_activity_log(self, mock_capture: MagicMock, mock_delete_bulky_postgres_data: MagicMock):
        self.organization_membership.level = OrganizationMembership.Level.ADMIN
        self.organization_membership.save()

        team: Team = Team.objects.create_with_data(organization=self.organization)

        response = self.client.delete(f"/api/projects/{team.id}")
        assert response.status_code == 204

        # activity log is queried in the context of the team
        # and the team was deleted, so we can't (for now) view a deleted team activity via the API
        # even though the activity log is recorded

        deleted_team_activity_response = self.client.get(f"/api/projects/{team.id}/activity")
        assert deleted_team_activity_response.status_code == status.HTTP_404_NOT_FOUND

        # we can't query by API but can prove the log was recorded
        activity = [a.__dict__ for a in ActivityLog.objects.filter(team_id=team.pk).all()]
        assert activity == [
            {
                "_state": ANY,
                "activity": "deleted",
                "created_at": ANY,
                "detail": {
                    "changes": None,
                    "name": "Default Project",
                    "short_id": None,
                    "trigger": None,
                    "type": None,
                },
                "id": ANY,
                "is_system": False,
                "organization_id": ANY,
                "team_id": team.pk,
                "item_id": str(team.pk),
                "scope": "Team",
                "user_id": self.user.pk,
                "was_impersonated": False,
            },
        ]

    @patch("posthog.api.team.delete_bulky_postgres_data")
    @patch("posthoganalytics.capture")
    def test_delete_team_own_second(self, mock_capture: MagicMock, mock_delete_bulky_postgres_data: MagicMock):
        self.organization_membership.level = OrganizationMembership.Level.ADMIN
        self.organization_membership.save()

        team: Team = Team.objects.create_with_data(organization=self.organization)

        self.assertEqual(Team.objects.filter(organization=self.organization).count(), 2)

        response = self.client.delete(f"/api/projects/{team.id}")

        self.assertEqual(response.status_code, 204)
        self.assertEqual(Team.objects.filter(organization=self.organization).count(), 1)
        self.assertEqual(
            AsyncDeletion.objects.filter(team_id=team.id, deletion_type=DeletionType.Team, key=str(team.id)).count(),
            1,
        )
        mock_capture.assert_has_calls(
            calls=[
                call(
                    self.user.distinct_id,
                    "membership level changed",
                    properties={"new_level": 8, "previous_level": 1, "$set": mock.ANY},
                    groups=mock.ANY,
                ),
                call(self.user.distinct_id, "team deleted", properties={}, groups=mock.ANY),
            ]
        )
        mock_delete_bulky_postgres_data.assert_called_once_with(team_ids=[team.pk])

    def test_delete_bulky_postgres_data(self):
        self.organization_membership.level = OrganizationMembership.Level.ADMIN
        self.organization_membership.save()

        team: Team = Team.objects.create_with_data(organization=self.organization)

        self.assertEqual(Team.objects.filter(organization=self.organization).count(), 2)

        from posthog.models.cohort import Cohort, CohortPeople
        from posthog.models.feature_flag.feature_flag import (
            FeatureFlag,
            FeatureFlagHashKeyOverride,
        )

        # from posthog.models.insight_caching_state import InsightCachingState
        from posthog.models.person import Person

        cohort = Cohort.objects.create(team=team, created_by=self.user, name="test")
        person = Person.objects.create(
            team=team,
            distinct_ids=["example_id"],
            properties={"email": "tim@posthog.com", "team": "posthog"},
        )
        person.add_distinct_id("test")
        flag = FeatureFlag.objects.create(
            team=team,
            name="test",
            key="test",
            rollout_percentage=50,
            created_by=self.user,
        )
        FeatureFlagHashKeyOverride.objects.create(
            team_id=team.pk,
            person_id=person.id,
            feature_flag_key=flag.key,
            hash_key="test",
        )
        CohortPeople.objects.create(cohort_id=cohort.pk, person_id=person.pk)
        EarlyAccessFeature.objects.create(
            team=team,
            name="Test flag",
            description="A fancy new flag.",
            stage="beta",
            feature_flag=flag,
        )

        # if something is missing then teardown fails
        response = self.client.delete(f"/api/projects/{team.id}")
        self.assertEqual(response.status_code, 204)

    def test_delete_batch_exports(self):
        self.organization_membership.level = OrganizationMembership.Level.ADMIN
        self.organization_membership.save()

        team: Team = Team.objects.create_with_data(organization=self.organization)

        destination_data = {
            "type": "S3",
            "config": {
                "bucket_name": "my-production-s3-bucket",
                "region": "us-east-1",
                "prefix": "posthog-events/",
                "aws_access_key_id": "abc123",
                "aws_secret_access_key": "secret",
            },
        }

        batch_export_data = {
            "name": "my-production-s3-bucket-destination",
            "destination": destination_data,
            "interval": "hour",
        }

        temporal = sync_connect()

        with start_test_worker(temporal):
            response = self.client.post(
                f"/api/projects/{team.id}/batch_exports",
                json.dumps(batch_export_data),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 201)

            batch_export = response.json()
            batch_export_id = batch_export["id"]

            response = self.client.delete(f"/api/projects/{team.id}")
            self.assertEqual(response.status_code, 204)

            response = self.client.get(f"/api/projects/{team.id}/batch_exports/{batch_export_id}")
            self.assertEqual(response.status_code, 404)

            with self.assertRaises(RPCError):
                describe_schedule(temporal, batch_export_id)

    @freeze_time("2022-02-08")
    def test_reset_token(self):
        self.organization_membership.level = OrganizationMembership.Level.ADMIN
        self.organization_membership.save()

        self._assert_activity_log_is_empty()

        self.team.api_token = "xyz"
        self.team.save()

        response = self.client.patch(f"/api/projects/{self.team.id}/reset_token/")
        response_data = response.json()

        self.team.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotEqual(response_data["api_token"], "xyz")
        self.assertEqual(response_data["api_token"], self.team.api_token)
        self.assertTrue(response_data["api_token"].startswith("phc_"))

        self._assert_activity_log(
            [
                {
                    "activity": "updated",
                    "created_at": "2022-02-08T00:00:00Z",
                    "detail": {
                        "changes": [
                            {
                                "action": "changed",
                                "after": None,
                                "before": None,
                                "field": "api_token",
                                "type": "Team",
                            },
                        ],
                        "name": "Default Project",
                        "short_id": None,
                        "trigger": None,
                        "type": None,
                    },
                    "item_id": str(self.team.pk),
                    "scope": "Team",
                    "user": {
                        "email": "user1@posthog.com",
                        "first_name": "",
                    },
                },
            ]
        )

    def test_reset_token_insufficient_priviledges(self):
        self.team.api_token = "xyz"
        self.team.save()

        response = self.client.patch(f"/api/projects/{self.team.id}/reset_token/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_update_primary_dashboard(self):
        d = Dashboard.objects.create(name="Test", team=self.team)

        # Can set it
        response = self.client.patch("/api/projects/@current/", {"primary_dashboard": d.id})
        response_data = response.json()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response_data["name"], self.team.name)
        self.assertEqual(response_data["primary_dashboard"], d.id)

    def test_cant_set_primary_dashboard_to_another_teams_dashboard(self):
        team_2 = Team.objects.create(organization=self.organization, name="Default Project")
        d = Dashboard.objects.create(name="Test", team=team_2)

        response = self.client.patch("/api/projects/@current/", {"primary_dashboard": d.id})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        response = self.client.get("/api/projects/@current/")
        response_data = response.json()
        self.assertEqual(response_data["primary_dashboard"], None)

    def test_update_timezone_remove_cache(self):
        # Seed cache with some insights
        self.client.post(
            f"/api/projects/{self.team.id}/insights/",
            data={"filters": {"events": json.dumps([{"id": "user signed up"}])}},
        )
        response = self.client.post(
            f"/api/projects/{self.team.id}/insights/",
            data={"filters": {"events": json.dumps([{"id": "$pageview"}])}},
        ).json()
        self.client.get(
            f"/api/projects/{self.team.id}/insights/trend/",
            data={"events": json.dumps([{"id": "$pageview"}])},
        )
        self.client.get(
            f"/api/projects/{self.team.id}/insights/trend/",
            data={"events": json.dumps([{"id": "user signed up"}])},
        )

        self.assertEqual(cache.get(response["filters_hash"])["result"][0]["count"], 0)
        self.client.patch(f"/api/projects/{self.team.id}/", {"timezone": "US/Pacific"})
        # Verify cache was deleted
        self.assertEqual(cache.get(response["filters_hash"]), None)

    def test_is_generating_demo_data(self):
        cache_key = f"is_generating_demo_data_{self.team.pk}"
        cache.set(cache_key, "True")
        response = self.client.get(f"/api/projects/{self.team.id}/is_generating_demo_data/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), {"is_generating_demo_data": True})
        cache.delete(cache_key)
        response = self.client.get(f"/api/projects/{self.team.id}/is_generating_demo_data/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), {"is_generating_demo_data": False})

    @patch("posthog.api.team.create_data_for_demo_team.delay")
    def test_org_member_can_create_demo_project(self, mock_create_data_for_demo_team: MagicMock):
        self.organization_membership.level = OrganizationMembership.Level.MEMBER
        self.organization_membership.save()
        response = self.client.post("/api/projects/", {"name": "Hedgebox", "is_demo": True})
        mock_create_data_for_demo_team.assert_called_once()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @freeze_time("2022-02-08")
    def test_team_float_config_can_be_serialized_to_activity_log(self):
        # regression test since this isn't true by default
        response = self.client.patch(f"/api/projects/@current/", {"session_recording_sample_rate": 0.4})
        assert response.status_code == status.HTTP_200_OK
        self._assert_activity_log(
            [
                {
                    "activity": "updated",
                    "created_at": "2022-02-08T00:00:00Z",
                    "detail": {
                        "changes": [
                            {
                                "action": "created",
                                "after": "0.4",
                                "before": None,
                                "field": "session_recording_sample_rate",
                                "type": "Team",
                            },
                        ],
                        "name": "Default Project",
                        "short_id": None,
                        "trigger": None,
                        "type": None,
                    },
                    "item_id": str(self.team.pk),
                    "scope": "Team",
                    "user": {
                        "email": "user1@posthog.com",
                        "first_name": "",
                    },
                },
            ]
        )

    @freeze_time("2022-02-08")
    def test_team_creation_is_in_activity_log(self):
        Team.objects.all().delete()

        self.organization_membership.level = OrganizationMembership.Level.ADMIN
        self.organization_membership.save()

        team_name = str(uuid.uuid4())
        response = self.client.post("/api/projects/", {"name": team_name, "is_demo": False})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        team_id = response.json()["id"]
        self._assert_activity_log(
            [
                {
                    "activity": "created",
                    "created_at": "2022-02-08T00:00:00Z",
                    "detail": {
                        "changes": None,
                        "name": team_name,
                        "short_id": None,
                        "trigger": None,
                        "type": None,
                    },
                    "item_id": str(team_id),
                    "scope": "Team",
                    "user": {
                        "email": "user1@posthog.com",
                        "first_name": "",
                    },
                },
            ],
            team_id=team_id,
        )

    def test_team_is_cached_on_create_and_update(self):
        Team.objects.all().delete()
        self.organization_membership.level = OrganizationMembership.Level.ADMIN
        self.organization_membership.save()

        response = self.client.post("/api/projects/", {"name": "Test", "is_demo": False})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.json()["name"], "Test")

        token = response.json()["api_token"]
        team_id = response.json()["id"]

        cached_team = get_team_in_cache(token)

        assert cached_team is not None
        self.assertEqual(cached_team.name, "Test")
        self.assertEqual(cached_team.uuid, response.json()["uuid"])
        self.assertEqual(cached_team.id, response.json()["id"])

        response = self.client.patch(
            f"/api/projects/{team_id}/",
            {"timezone": "Europe/Istanbul", "session_recording_opt_in": True},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        cached_team = get_team_in_cache(token)
        assert cached_team is not None

        self.assertEqual(cached_team.name, "Test")
        self.assertEqual(cached_team.uuid, response.json()["uuid"])
        self.assertEqual(cached_team.session_recording_opt_in, True)

        # only things in CachedTeamSerializer are cached!
        self.assertEqual(cached_team.timezone, "UTC")

        # reset token should update cache as well
        response = self.client.patch(f"/api/projects/{team_id}/reset_token/")
        response_data = response.json()

        cached_team = get_team_in_cache(token)
        assert cached_team is None

        cached_team = get_team_in_cache(response_data["api_token"])
        assert cached_team is not None
        self.assertEqual(cached_team.name, "Test")
        self.assertEqual(cached_team.uuid, response.json()["uuid"])
        self.assertEqual(cached_team.session_recording_opt_in, True)

    def test_turn_on_exception_autocapture(self):
        response = self.client.get("/api/projects/@current/")
        assert response.json()["autocapture_exceptions_opt_in"] is None

        response = self.client.patch(
            "/api/projects/@current/",
            {"autocapture_exceptions_opt_in": "Welwyn Garden City"},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["detail"] == "Must be a valid boolean."

        response = self.client.patch("/api/projects/@current/", {"autocapture_exceptions_opt_in": True})
        assert response.status_code == status.HTTP_200_OK
        response = self.client.get("/api/projects/@current/")
        assert response.json()["autocapture_exceptions_opt_in"] is True

    def test_configure_exception_autocapture_event_dropping(self):
        response = self.client.get("/api/projects/@current/")
        assert response.json()["autocapture_exceptions_errors_to_ignore"] is None

        response = self.client.patch(
            "/api/projects/@current/",
            {"autocapture_exceptions_errors_to_ignore": {"wat": "am i"}},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["detail"] == "Must provide a list for field: autocapture_exceptions_errors_to_ignore."

        response = self.client.patch(
            "/api/projects/@current/",
            {"autocapture_exceptions_errors_to_ignore": [1, False]},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert (
            response.json()["detail"]
            == "Must provide a list of strings to field: autocapture_exceptions_errors_to_ignore."
        )

        response = self.client.patch(
            "/api/projects/@current/",
            {"autocapture_exceptions_errors_to_ignore": ["wat am i"]},
        )
        assert response.status_code == status.HTTP_200_OK
        response = self.client.get("/api/projects/@current/")
        assert response.json()["autocapture_exceptions_errors_to_ignore"] == ["wat am i"]

    def test_configure_exception_autocapture_event_dropping_only_allows_simple_config(self):
        response = self.client.patch(
            "/api/projects/@current/",
            {"autocapture_exceptions_errors_to_ignore": ["abc" * 300]},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert (
            response.json()["detail"]
            == "Field autocapture_exceptions_errors_to_ignore must be less than 300 characters. Complex config should be provided in posthog-js initialization."
        )

    @parameterized.expand(
        [
            [
                "non numeric string",
                "Welwyn Garden City",
                "invalid_input",
                "A valid number is required.",
            ],
            [
                "negative number",
                "-1",
                "min_value",
                "Ensure this value is greater than or equal to 0.",
            ],
            [
                "greater than one",
                "1.5",
                "max_value",
                "Ensure this value is less than or equal to 1.",
            ],
            [
                "too many digits",
                "0.534",
                "max_decimal_places",
                "Ensure that there are no more than 2 decimal places.",
            ],
        ]
    )
    def test_invalid_session_recording_sample_rates(
        self, _name: str, provided_value: str, expected_code: str, expected_error: str
    ) -> None:
        response = self.client.patch("/api/projects/@current/", {"session_recording_sample_rate": provided_value})
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json() == {
            "attr": "session_recording_sample_rate",
            "code": expected_code,
            "detail": expected_error,
            "type": "validation_error",
        }

    @parameterized.expand(
        [
            [
                "non numeric string",
                "Trentham monkey forest",
                "invalid_input",
                "A valid integer is required.",
            ],
            [
                "negative number",
                "-1",
                "min_value",
                "Ensure this value is greater than or equal to 0.",
            ],
            [
                "greater than 15000",
                "15001",
                "max_value",
                "Ensure this value is less than or equal to 15000.",
            ],
            ["too many digits", "0.5", "invalid_input", "A valid integer is required."],
        ]
    )
    def test_invalid_session_recording_minimum_duration(
        self, _name: str, provided_value: str, expected_code: str, expected_error: str
    ) -> None:
        response = self.client.patch(
            "/api/projects/@current/",
            {"session_recording_minimum_duration_milliseconds": provided_value},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json() == {
            "attr": "session_recording_minimum_duration_milliseconds",
            "code": expected_code,
            "detail": expected_error,
            "type": "validation_error",
        }

    @parameterized.expand(
        [
            [
                "string",
                "Marple bridge",
                "invalid_input",
                "Must provide a dictionary or None.",
            ],
            ["numeric string", "-1", "invalid_input", "Must provide a dictionary or None."],
            ["numeric", 1, "invalid_input", "Must provide a dictionary or None."],
            ["numeric positive string", "1", "invalid_input", "Must provide a dictionary or None."],
            [
                "unexpected json - no id",
                {"key": "something"},
                "invalid_input",
                "Must provide a dictionary with only 'id' and 'key' keys. _or_ only 'id', 'key', and 'variant' keys.",
            ],
            [
                "unexpected json - no key",
                {"id": 1},
                "invalid_input",
                "Must provide a dictionary with only 'id' and 'key' keys. _or_ only 'id', 'key', and 'variant' keys.",
            ],
            [
                "unexpected json - only variant",
                {"variant": "1"},
                "invalid_input",
                "Must provide a dictionary with only 'id' and 'key' keys. _or_ only 'id', 'key', and 'variant' keys.",
            ],
            [
                "unexpected json - variant must be string",
                {"variant": 1},
                "invalid_input",
                "Must provide a dictionary with only 'id' and 'key' keys. _or_ only 'id', 'key', and 'variant' keys.",
            ],
            [
                "unexpected json - missing id",
                {"key": "one", "variant": "1"},
                "invalid_input",
                "Must provide a dictionary with only 'id' and 'key' keys. _or_ only 'id', 'key', and 'variant' keys.",
            ],
            [
                "unexpected json - missing key",
                {"id": "one", "variant": "1"},
                "invalid_input",
                "Must provide a dictionary with only 'id' and 'key' keys. _or_ only 'id', 'key', and 'variant' keys.",
            ],
            [
                "unexpected json - neither",
                {"wat": "wat"},
                "invalid_input",
                "Must provide a dictionary with only 'id' and 'key' keys. _or_ only 'id', 'key', and 'variant' keys.",
            ],
        ]
    )
    def test_invalid_session_recording_linked_flag(
        self, _name: str, provided_value: Any, expected_code: str, expected_error: str
    ) -> None:
        response = self._patch_linked_flag_config(provided_value, expected_status=status.HTTP_400_BAD_REQUEST)

        assert response.json() == {
            "attr": "session_recording_linked_flag",
            "code": expected_code,
            "detail": expected_error,
            "type": "validation_error",
        }

    def test_can_set_and_unset_session_recording_linked_flag(self) -> None:
        self._patch_linked_flag_config({"id": 1, "key": "provided_value"})
        self._assert_linked_flag_config({"id": 1, "key": "provided_value"})

        self._patch_linked_flag_config(None)
        self._assert_linked_flag_config(None)

    def test_can_set_and_unset_session_recording_linked_flag_variant(self) -> None:
        self._patch_linked_flag_config({"id": 1, "key": "provided_value", "variant": "test"})
        self._assert_linked_flag_config({"id": 1, "key": "provided_value", "variant": "test"})

        self._patch_linked_flag_config(None)
        self._assert_linked_flag_config(None)

    @parameterized.expand(
        [
            [
                "string",
                "Marple bridge",
                "invalid_input",
                "Must provide a dictionary or None.",
            ],
            ["numeric", "-1", "invalid_input", "Must provide a dictionary or None."],
            [
                "unexpected json - no recordX",
                {"key": "something"},
                "invalid_input",
                "Must provide a dictionary with only 'recordHeaders' and/or 'recordBody' keys.",
            ],
        ]
    )
    def test_invalid_session_recording_network_payload_capture_config(
        self, _name: str, provided_value: str, expected_code: str, expected_error: str
    ) -> None:
        response = self.client.patch(
            "/api/projects/@current/", {"session_recording_network_payload_capture_config": provided_value}
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json() == {
            "attr": "session_recording_network_payload_capture_config",
            "code": expected_code,
            "detail": expected_error,
            "type": "validation_error",
        }

    def test_can_set_and_unset_session_recording_network_payload_capture_config(self) -> None:
        # can set just one
        first_patch_response = self.client.patch(
            "/api/projects/@current/",
            {"session_recording_network_payload_capture_config": {"recordHeaders": True}},
        )
        assert first_patch_response.status_code == status.HTTP_200_OK
        get_response = self.client.get("/api/projects/@current/")
        assert get_response.json()["session_recording_network_payload_capture_config"] == {"recordHeaders": True}

        # can set the other
        first_patch_response = self.client.patch(
            "/api/projects/@current/",
            {"session_recording_network_payload_capture_config": {"recordBody": False}},
        )
        assert first_patch_response.status_code == status.HTTP_200_OK
        get_response = self.client.get("/api/projects/@current/")
        assert get_response.json()["session_recording_network_payload_capture_config"] == {"recordBody": False}

        # can unset both
        response = self.client.patch(
            "/api/projects/@current/", {"session_recording_network_payload_capture_config": None}
        )
        assert response.status_code == status.HTTP_200_OK
        second_get_response = self.client.get("/api/projects/@current/")
        assert second_get_response.json()["session_recording_network_payload_capture_config"] is None

    def test_can_set_and_unset_session_replay_config(self) -> None:
        # can set
        self._patch_session_replay_config({"record_canvas": True})
        self._assert_replay_config_is({"record_canvas": True})

        # can unset
        self._patch_session_replay_config(None)
        self._assert_replay_config_is(None)

    @parameterized.expand(
        [
            [
                "string",
                "Marple bridge",
                "invalid_input",
                "Must provide a dictionary or None.",
            ],
            ["numeric", "-1", "invalid_input", "Must provide a dictionary or None."],
            [
                "unexpected json - no record",
                {"key": "something"},
                "invalid_input",
                "Must provide a dictionary with only allowed keys: included_event_properties, opt_in, preferred_events, excluded_events, important_user_properties.",
            ],
        ]
    )
    def test_invalid_session_replay_config_ai_config(
        self, _name: str, provided_value: str, expected_code: str, expected_error: str
    ) -> None:
        response = self._patch_session_replay_config(
            {"ai_config": provided_value}, expected_status=status.HTTP_400_BAD_REQUEST
        )
        assert response.json() == {
            "attr": "session_replay_config",
            "code": expected_code,
            "detail": expected_error,
            "type": "validation_error",
        }

    def test_can_set_and_unset_session_replay_config_ai_config(self) -> None:
        # can set just the opt-in
        self._patch_session_replay_config({"ai_config": {"opt_in": True}})
        self._assert_replay_config_is({"ai_config": {"opt_in": True}})

        # can set some preferences
        self._patch_session_replay_config({"ai_config": {"opt_in": False, "included_event_properties": ["something"]}})
        self._assert_replay_config_is({"ai_config": {"opt_in": False, "included_event_properties": ["something"]}})

        self._patch_session_replay_config({"ai_config": None})
        self._assert_replay_config_is({"ai_config": None})

    def test_can_set_replay_configs_without_providing_them_all(self) -> None:
        # can set just the opt-in
        self._patch_session_replay_config({"ai_config": {"opt_in": True}})
        self._assert_replay_config_is({"ai_config": {"opt_in": True}})

        self._patch_session_replay_config({"record_canvas": True})
        self._assert_replay_config_is({"record_canvas": True, "ai_config": {"opt_in": True}})

    def test_can_set_replay_configs_without_providing_them_all_even_when_either_side_is_none(self) -> None:
        # because we do some dictionary copying we need a regression test to ensure we can always set and unset keys
        self._patch_session_replay_config({"record_canvas": True, "ai_config": {"opt_in": True}})
        self._assert_replay_config_is({"record_canvas": True, "ai_config": {"opt_in": True}})

        self._patch_session_replay_config({"record_canvas": None})
        self._assert_replay_config_is({"record_canvas": None, "ai_config": {"opt_in": True}})

        # top-level from having a value to None
        self._patch_session_replay_config(None)
        self._assert_replay_config_is(None)

        # top-level from None to having a value
        self._patch_session_replay_config({"ai_config": None})
        self._assert_replay_config_is({"ai_config": None})

        # next-level from None to having a value
        self._patch_session_replay_config({"ai_config": {"opt_in": True}})
        self._assert_replay_config_is({"ai_config": {"opt_in": True}})

        # next-level from having a value to None
        self._patch_session_replay_config({"ai_config": None})
        self._assert_replay_config_is({"ai_config": None})

    def test_can_set_replay_configs_patch_session_replay_config_one_level_deep(self) -> None:
        # can set just the opt-in
        self._patch_session_replay_config({"ai_config": {"opt_in": True}})
        self._assert_replay_config_is({"ai_config": {"opt_in": True}})

        self._patch_session_replay_config({"ai_config": {"included_event_properties": ["something"]}})
        # even though opt_in was not provided in the patch it should be preserved
        self._assert_replay_config_is({"ai_config": {"opt_in": True, "included_event_properties": ["something"]}})

        self._patch_session_replay_config({"ai_config": {"opt_in": None, "included_event_properties": ["something"]}})
        # even though opt_in was not provided in the patch it should be preserved
        self._assert_replay_config_is({"ai_config": {"opt_in": None, "included_event_properties": ["something"]}})

        # but we don't go into the next nested level and patch that data
        # sending a new value without the original
        self._patch_session_replay_config({"ai_config": {"included_event_properties": ["and another"]}})
        # and the existing second level nesting is not preserved
        self._assert_replay_config_is({"ai_config": {"opt_in": None, "included_event_properties": ["and another"]}})

    def _assert_replay_config_is(self, expected: Dict[str, Any] | None) -> HttpResponse:
        get_response = self.client.get("/api/projects/@current/")
        assert get_response.status_code == status.HTTP_200_OK, get_response.json()
        assert get_response.json()["session_replay_config"] == expected

        return get_response

    def _patch_session_replay_config(
        self, config: Dict[str, Any] | None, expected_status: int = status.HTTP_200_OK
    ) -> HttpResponse:
        patch_response = self.client.patch(
            "/api/projects/@current/",
            {"session_replay_config": config},
        )
        assert patch_response.status_code == expected_status, patch_response.json()

        return patch_response

    def _assert_linked_flag_config(self, expected_config: Dict | None) -> HttpResponse:
        response = self.client.get("/api/projects/@current/")
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["session_recording_linked_flag"] == expected_config
        return response

    def _patch_linked_flag_config(self, config: Dict | None, expected_status: int = status.HTTP_200_OK) -> HttpResponse:
        response = self.client.patch("/api/projects/@current/", {"session_recording_linked_flag": config})
        assert response.status_code == expected_status, response.json()
        return response


def create_team(organization: Organization, name: str = "Test team") -> Team:
    """
    This is a helper that just creates a team. It currently uses the orm, but we
    could use either the api, or django admin to create, to get better parity
    with real world  scenarios.
    """
    return Team.objects.create(
        organization=organization,
        name=name,
        ingested_event=True,
        completed_snippet_onboarding=True,
        is_demo=True,
    )


async def acreate_team(organization: Organization, name: str = "Test team") -> Team:
    """
    This is a helper that just creates a team. It currently uses the orm, but we
    could use either the api, or django admin to create, to get better parity
    with real world  scenarios.
    """
    return await sync_to_async(create_team)(organization, name=name)
