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


def test_heatmap_fallback_generates_synthetic_points_without_coordinates():
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
        assert payload["points"]
        assert payload["metadata"]["using_synthetic_points"] is True
        assert payload["metadata"]["has_coordinates"] is True
        assert payload["points"][0].get("synthetic") is True

        db.session.remove()
        db.drop_all()
