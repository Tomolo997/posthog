from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models

from posthog.models.utils import UUIDModel


class EnterpriseTaggedItem(UUIDModel):
    """
    Taggable describes global tag-object relationships.

    Note: This is an EE only feature, however the model exists in posthog so that it is backwards accessible from all
    models. Whether we should be able to interact with this table is determined in the `TaggedItemSerializer` which
    imports `EnterpriseTaggedItemSerializer` if the feature is available.

    Today, tags exist at the model-level making it impossible to aggregate, filter, and query objects appwide by tags.
    We want to deprecate model-specific tags and refactor tag relationships into a separate table that keeps track of
    tag-object relationships.

    Models that had in-line tags before this table was created:
    - ee/models/ee_event_definition.py
    - ee/models/ee_property_definition.py
    - models/dashboard.py
    - models/insight.py

    Models that are taggable throughout the app:
    - models/dashboard.py
    - models/event_definition.py
    - models/property_definition.py
    - models/insight.py

    https://docs.djangoproject.com/en/4.0/ref/contrib/contenttypes/#generic-relations
    """

    tag: models.SlugField = models.SlugField()
    team: models.ForeignKey = models.ForeignKey("Team", on_delete=models.CASCADE)
    color: models.CharField = models.CharField(max_length=400, null=True, blank=True)

    content_type: models.ForeignKey = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    # Primary key value of related model. Query by this to get all tags for specific model. This is a charfield because
    # there we don't have a standard way of storing objects. Some models use positive integer ids and others use UUID's.
    object_id: models.CharField = models.CharField(max_length=400)
    content_object: GenericForeignKey = GenericForeignKey("content_type", "object_id")

    class Meta:
        unique_together = ("content_type", "object_id", "tag")

    def __str__(self):
        return self.tag
