from services.analytics.ingestor import analytics_ingestor
from services.qdrant_service import get_qdrant_client
from models import AnalyticsEventV2
from app import create_app, db
from config import TestingConfig

def test_analytics_ingestion():
    app = create_app(TestingConfig)

    with app.app_context():
        db.create_all()
        AnalyticsEventV2.query.delete()
        db.session.commit()

        # Track event
        analytics_ingestor.track(
            tenant_id=1,
            event_name="test_event_v2",
            payload={"foo": "bar"},
            user_id=123,
            channel="web_widget"
        )

        # Verify
        event = AnalyticsEventV2.query.filter_by(event_name="test_event_v2").first()
        assert event is not None
        assert event.event_name == "test_event_v2"
        assert event.tenant_id == 1
        assert event.channel == "web_widget"
        print("Analytics ingestion test passed!")

def test_qdrant_interface():
    # Only checks import and basic client init, not connection (requires real Qdrant)
    client = get_qdrant_client()
    # It might be None if env vars are missing, which is expected in this env
    print(f"Qdrant client init: {client}")

if __name__ == "__main__":
    test_analytics_ingestion()
    test_qdrant_interface()
