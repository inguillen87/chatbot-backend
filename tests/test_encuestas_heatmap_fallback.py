import os
from datetime import datetime, timezone

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import EncEncuesta, EncRespuesta
from services.encuestas_analytics_service import get_heatmap


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}


def test_heatmap_without_coordinates_returns_empty_by_default():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        encuesta = EncEncuesta(
            tenant_id=1,
            slug="encuesta-sin-coordenadas",
            titulo="Encuesta sin coordenadas",
            estado="publicada",
            tipo="opinion",
        )
        db.session.add(encuesta)
        db.session.commit()

        respuesta = EncRespuesta(
            encuesta_id=encuesta.id,
            tenant_id=encuesta.tenant_id,
            canal="web",
            barrio="Centro",
            ciudad="Junín",
            provincia="Buenos Aires",
            pais="Argentina",
            submitted_at=datetime.now(timezone.utc),
        )
        db.session.add(respuesta)
        db.session.commit()

        payload = get_heatmap(encuesta.id)
        assert payload["points"] == []
        assert payload["cells"] == []
        assert payload["metadata"]["using_synthetic_points"] is False
        assert payload["metadata"]["has_coordinates"] is False
        assert payload["metadata"]["can_render_heatmap"] is False
        assert payload["metadata"]["empty_reason"] == "no_real_geo_points"
        assert payload["render_contract"]["state"] == "empty"
        assert payload["render_contract"]["can_render_heatmap"] is False
        assert payload["ai_insights"]["contract_version"] == "huggingface.ai_insights.v1"
        assert payload["ai_layers"]["contract_version"] == "huggingface.map_ai_layers.v1"
        assert payload["map_experience"]["preferred_visualization"] == "interactive_globe_heatmap"
        assert payload["metadata"]["map_layers"]["ai_risk"]["kind"] == "ai_risk"
        assert "interactive_globe" in payload["render_contract"]["recommended_views"]

        synthetic_payload = get_heatmap(encuesta.id, {"allow_synthetic_geo": True})
        assert synthetic_payload["points"]
        assert synthetic_payload["metadata"]["using_synthetic_points"] is True
        assert synthetic_payload["points"][0].get("synthetic") is True
        assert synthetic_payload["ai_layers"]["layers"]["survey_participation"]["count"] >= 1

        db.session.remove()
        db.drop_all()
