import logging
import uuid
from datetime import datetime, timezone
from models import db, AnalyticsEventV2
from flask import g

logger = logging.getLogger(__name__)

class AnalyticsIngestor:
    """
    Centralized service to ingest high-volume events into Postgres V2 Event Store.
    Designed to be lightweight and non-blocking (ideally async, but sync for now).
    """

    def track(self, tenant_id: int, event_name: str, payload: dict = None, **kwargs):
        """
        Track an analytics event.

        Args:
            tenant_id (int): The ID of the tenant owning the event.
            event_name (str): Standardized event name (e.g. 'order_created').
            payload (dict): Arbitrary metadata (json).
            **kwargs: standard fields (user_id, channel, lat, lng, etc.)
        """
        commit = kwargs.get("commit", True)
        raise_on_error = kwargs.get("raise_on_error", False)
        try:
            if not tenant_id:
                message = f"[Analytics] Event {event_name} dropped: missing tenant_id"
                if raise_on_error:
                    raise ValueError(message)
                logger.warning(message)
                return None

            requested_event_id = kwargs.get("event_id")
            event_id = str(requested_event_id).strip() if requested_event_id else str(uuid.uuid4())
            if not event_id or len(event_id) > 36:
                raise ValueError("analytics event_id must contain at most 36 characters")

            existing = db.session.get(AnalyticsEventV2, event_id)
            if existing is not None:
                return existing

            event = AnalyticsEventV2(
                id=event_id,
                ts=datetime.now(timezone.utc),
                tenant_id=tenant_id,
                event_name=event_name,
                metadata_payload=payload or {}
            )

            # Fill standard optional fields
            event.user_id = kwargs.get('user_id')
            event.anon_id = kwargs.get('anon_id')
            event.channel = kwargs.get('channel')
            event.session_id = kwargs.get('session_id')
            event.lat = kwargs.get('lat')
            event.lng = kwargs.get('lng')
            event.entity_ref = kwargs.get('entity_ref')
            event.tenant_type = kwargs.get('tenant_type', 'pyme') # Default

            db.session.add(event)
            if commit:
                db.session.commit()
            return event

        except Exception as e:
            logger.error(f"[Analytics] Failed to ingest event {event_name}: {e}")
            if commit:
                db.session.rollback()
            if raise_on_error:
                raise
            return None

analytics_ingestor = AnalyticsIngestor()
