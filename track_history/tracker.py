from __future__ import unicode_literals
import threading

from django.contrib.contenttypes.models import ContentType
from django.db.models import FileField
from django.db.models.query_utils import DeferredAttribute
from django.utils.encoding import force_text
from track_history.signals import save_signals, delete_signals

from .models import TrackHistoryRecord
from .utils import has_int_pk


class TrackHelper(object):
    thread = threading.local()
    initial_values = {}

    def __init__(self, tracked_instance, fields, exclude):
        self.tracked_instance = tracked_instance
        self.fields = fields
        self.exclude = exclude

        self.store_current_state()

    def prepare_attribute_for_json(self, field):
        if hasattr(self.tracked_instance, '_th_prepare_%s_for_json' % field.name):
            return getattr(self.tracked_instance, '_th_prepare_%s_for_json' % field.name)()
        elif hasattr(self.tracked_instance, '_th_prepare_attribute_for_json'):
            return getattr(self.tracked_instance, '_th_prepare_attribute_for_json')(field)

        # It's always safe to access the field attribute name, it refers to simple types that are immediately
        # available on the instance.
        value = getattr(self.tracked_instance, field.attname)
        if isinstance(field, FileField):
            return value.url if value else None

        return value

    def get_current_state(self):
        """Returns a ``field -> value`` dict of the current state of the instance."""
        fields = {}
        for field in self.get_tracked_fields():
            fields[field.attname] = self.prepare_attribute_for_json(field)

        return fields

    def store_current_state(self, record_type=None):
        if record_type == TrackHistoryRecord.RECORD_TYPES.deleted or not self.tracked_instance.pk:
            self.initial_values = {}
        else:
            self.initial_values = self.get_current_state()

    def get_tracked_fields(self):
        fields = self.tracked_instance._meta.concrete_model._meta.local_fields

        if self.tracked_instance._deferred:
            fields = filter(lambda field: not isinstance(
                self.tracked_instance.__class__.__dict__.get(field.attname), DeferredAttribute), fields)

        if self.fields:
            fields = filter(lambda x: x.name in self.fields, fields)

        if self.exclude:
            fields = filter(lambda x: x.name not in self.exclude, fields)

        return fields

    def changes(self):
        """
        Returns a ``field -> (previous value, current value)`` dict of changes
        from the previous state to the current state.
        """
        current = self.get_current_state()
        return {key: (was, current[key]) for key, was in self.initial_values.iteritems() if was != current[key]}

    def signal_receiver(self, instance, signal, **kwargs):
        if self.tracked_instance is not instance:
            raise AssertionError('Something is wrong with tracked instance, got different object then expected.')

        record_type = TrackHistoryRecord.RECORD_TYPES.modified

        if signal in save_signals:
            if kwargs.get('created', False):
                record_type = TrackHistoryRecord.RECORD_TYPES.created
        elif signal in delete_signals:
            record_type = TrackHistoryRecord.RECORD_TYPES.deleted

        self.create_history_track_record(record_type, kwargs.get('using', None))

    def get_history_track_data(self, record_type, db=None):
        """Creates the version data to be saved to the history track record model."""
        object_id = force_text(self.tracked_instance.pk)
        content_type = ContentType.objects.db_manager(db).get_for_model(self.tracked_instance)
        if has_int_pk(self.tracked_instance.__class__):
            object_id_int = int(self.tracked_instance.pk)
        else:
            object_id_int = None
        return {
            "object_id": object_id,
            "object_id_int": object_id_int,
            "content_type": content_type,
            "record_type": record_type,
            "changes": self.changes(),
            "user": self.get_related_user()
        }

    def create_history_track_record(self, record_type, db=None):
        record_data = self.get_history_track_data(record_type, db)
        TrackHistoryRecord.objects.create(**record_data)
        self.store_current_state(record_type)

    def get_related_user(self):
        """Get the modifying user from middleware."""
        try:
            if self.thread.user.is_authenticated():
                return self.thread.user
            return None
        except AttributeError:
            return None
