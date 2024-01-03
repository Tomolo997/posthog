from typing import Any, List

from posthog.temporal.common.client import sync_connect
from posthog.batch_exports.service import batch_export_delete_schedule


def delete_bulky_postgres_data(team_ids: List[int]):
    "Efficiently delete large tables for teams from postgres. Using normal CASCADE delete here can time out"

    from posthog.models.cohort import CohortPeople
    from posthog.models.feature_flag.feature_flag import FeatureFlagHashKeyOverride
    from posthog.models.insight_caching_state import InsightCachingState
    from posthog.models.person import Person, PersonDistinctId
    from posthog.models.early_access_feature import EarlyAccessFeature

    _raw_delete(EarlyAccessFeature.objects.filter(team_id__in=team_ids))
    _raw_delete(PersonDistinctId.objects.filter(team_id__in=team_ids))
    _raw_delete(CohortPeople.objects.filter(cohort__team_id__in=team_ids))
    _raw_delete(FeatureFlagHashKeyOverride.objects.filter(team_id__in=team_ids))
    _raw_delete(Person.objects.filter(team_id__in=team_ids))
    _raw_delete(InsightCachingState.objects.filter(team_id__in=team_ids))


def _raw_delete(queryset: Any):
    "Issues a single DELETE statement for the queryset"
    queryset._raw_delete(queryset.db)


def delete_batch_exports(team_ids: List[int]):
    """Delete BatchExports for deleted teams.

    Using normal CASCADE doesn't trigger a delete from Temporal.
    """
    from posthog.batch_exports.models import BatchExport

    temporal = sync_connect()

    for batch_export in BatchExport.objects.filter(team_id__in=team_ids):
        schedule_id = batch_export.id

        batch_export.delete()
        batch_export.destination.delete()

        batch_export_delete_schedule(temporal, str(schedule_id))
