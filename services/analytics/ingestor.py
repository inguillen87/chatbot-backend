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
        try:
            if not tenant_id:
                logger.warning(f"[Analytics] Event {event_name} dropped: missing tenant_id")
                return

            event = AnalyticsEventV2(
                id=str(uuid.uuid4()),
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
            db.session.commit()

        except Exception as e:
            logger.error(f"[Analytics] Failed to ingest event {event_name}: {e}")
            db.session.rollback()

analytics_ingestor = AnalyticsIngestor()
