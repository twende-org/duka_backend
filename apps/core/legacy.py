"""Helpers for rows that predate Django and keep their Firestore document id.

Rows imported from Firestore store that id in ``legacy_id``; the app treats it as
the visible id (``legacyId`` in API payloads). Requests may therefore arrive with
either a Django UUID or a Firestore id, and the API resolves both.
"""
import uuid

from django.core.exceptions import FieldError
from django.http import Http404
from rest_framework import serializers


def is_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def has_legacy_id(model) -> bool:
    """Whether the model carries a Firestore id column.

    ``User`` predates Firestore and is addressed by its uuid everywhere, so a
    non-uuid reference to it is not a legacy id and must not be queried as one.
    """
    try:
        model._meta.get_field('legacy_id')
    except FieldError:
        return False
    return True


def app_id(instance) -> str:
    """The id the app already knows the row by: legacy Firestore id, else uuid."""
    return getattr(instance, 'legacy_id', None) or str(instance.pk)


def resolve_legacy_pk(model, value):
    """Return ``value`` when it is a UUID, else the pk of the matching legacy row.

    Returns None when the value matches no row, so callers can decide between an
    empty result set (filters) and a 404 (detail routes).
    """
    if value in (None, ''):
        return None
    if is_uuid(value):
        return str(value)
    if not has_legacy_id(model):
        return None
    return model.objects.filter(legacy_id=str(value)).values_list('pk', flat=True).first()


def filter_by_ref(queryset, field, value, model):
    """Filter ``field`` by a UUID or legacy id; unknown values match nothing."""
    pk = resolve_legacy_pk(model, value)
    if pk is None:
        return queryset.none()
    return queryset.filter(**{field: pk})


class LegacyPrimaryKeyRelatedField(serializers.PrimaryKeyRelatedField):
    """Accepts a UUID pk or a Firestore legacy id for the referenced model.

    Responses mirror back the same app-visible id (``legacy_id`` when the row
    came from Firestore), so payloads keep the shape the app already stores.
    """

    def use_pk_only_optimization(self):
        # DRF's shortcut would hand us a PKOnlyObject carrying only the uuid,
        # hiding ``legacy_id`` from to_representation below.
        return False

    def to_internal_value(self, data):
        if not is_uuid(data):
            pk = resolve_legacy_pk(self.queryset.model, data)
            if pk is not None:
                data = pk
        return super().to_internal_value(data)

    def to_representation(self, value):
        legacy_id = getattr(value, 'legacy_id', None)
        if legacy_id:
            return legacy_id
        return super().to_representation(value)


class LegacyLookupMixin:
    """Resolves a Firestore id in the URL pk slot before DRF looks the row up.

    Without this a non-UUID pk would blow up inside the UUIDField conversion
    instead of returning a clean 404.
    """

    def get_object(self):
        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
        value = self.kwargs.get(lookup_url_kwarg)
        if value and not is_uuid(value):
            pk = resolve_legacy_pk(self.get_queryset().model, value)
            if pk is None:
                raise Http404
            self.kwargs[lookup_url_kwarg] = str(pk)
        return super().get_object()
