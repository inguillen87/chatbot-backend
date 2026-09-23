import os
from datetime import datetime, timezone

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import (
    EncEncuesta,
    EncLink,
    EncOpcion,
    EncPregunta,
    EncRespuesta,
    EncRespuestaDetalle,
    TenantProfile,
    User,
)
from services.encuestas_analytics_service import get_heatmap, _build_survey_publication_contract


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


def test_heatmap_applies_bbox_filter_to_real_survey_coordinates():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        encuesta = EncEncuesta(
            tenant_id=1,
            slug="encuesta-con-bbox",
            titulo="Encuesta con bbox",
            estado="publicada",
            tipo="opinion",
        )
        db.session.add(encuesta)
        db.session.commit()

        inside = EncRespuesta(
            encuesta_id=encuesta.id,
            tenant_id=encuesta.tenant_id,
            canal="web",
            barrio="Centro",
            ciudad="Junin",
            provincia="Mendoza",
            pais="Argentina",
            lat=-32.9205,
            lng=-68.8122,
            submitted_at=datetime.now(timezone.utc),
        )
        outside = EncRespuesta(
            encuesta_id=encuesta.id,
            tenant_id=encuesta.tenant_id,
            canal="whatsapp",
            barrio="Norte",
            ciudad="Junin",
            provincia="Mendoza",
            pais="Argentina",
            lat=-32.8701,
            lng=-68.7601,
            submitted_at=datetime.now(timezone.utc),
        )
        db.session.add_all([inside, outside])
        db.session.commit()

        payload = get_heatmap(encuesta.id, {"bbox": "-68.83,-32.94,-68.80,-32.90"})

        assert payload["render_contract"]["state"] == "ready"
        assert len(payload["points"]) == 1
        assert payload["points"][0]["barrio"] == "Centro"
        assert payload["points"][0]["canal"] == "web"
        assert len(payload["cells"]) == 1
        assert payload["metadata"]["has_coordinates"] is True
        assert payload["metadata"]["using_synthetic_points"] is False

        db.session.remove()
        db.drop_all()


def test_heatmap_preserves_response_category_in_points_and_maplibre_layers():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        encuesta = EncEncuesta(
            tenant_id=1,
            slug="encuesta-categorias-territoriales",
            titulo="Prioridades territoriales",
            estado="publicada",
            tipo="opinion",
        )
        db.session.add(encuesta)
        db.session.flush()
        pregunta = EncPregunta(
            encuesta_id=encuesta.id,
            orden=1,
            texto="¿Qué tema necesita atención?",
            tipo="opcion_unica",
            obligatoria=True,
        )
        db.session.add(pregunta)
        db.session.flush()
        luminarias = EncOpcion(
            pregunta_id=pregunta.id,
            orden=1,
            texto="Luminarias",
            valor="luminarias",
        )
        db.session.add(luminarias)
        db.session.flush()
        respuesta = EncRespuesta(
            encuesta_id=encuesta.id,
            tenant_id=encuesta.tenant_id,
            canal="whatsapp",
            barrio="Centro",
            ciudad="Junín",
            provincia="Buenos Aires",
            pais="Argentina",
            lat=-34.5838,
            lng=-60.9433,
            submitted_at=datetime.now(timezone.utc),
        )
        db.session.add(respuesta)
        db.session.flush()
        db.session.add(
            EncRespuestaDetalle(
                respuesta_id=respuesta.id,
                pregunta_id=pregunta.id,
                opcion_id=luminarias.id,
            )
        )
        db.session.commit()

        payload = get_heatmap(encuesta.id)

        assert payload["points"][0]["categoria"] == "Luminarias"
        assert payload["points"][0]["feature"]["properties"]["categoria"] == "Luminarias"
        categories = payload["metadata"]["category_layers"]["categories"]
        assert [category["categoria"] for category in categories] == ["luminarias"]
        feature = payload["metadata"]["category_layers"]["source"]["features"][0]
        assert feature["properties"]["categoria"] == "luminarias"

        db.session.remove()
        db.drop_all()


def test_government_survey_heatmap_exposes_boundary_evidence_per_point():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        owner = User(
            name="Admin territorial",
            email="heatmap-government@test.local",
            rol="admin",
            tenant_slug="junin",
        )
        owner.set_password("secret123")
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(
            slug="junin",
            nombre="Municipalidad de Junín",
            tipo="municipio",
            municipio_id=owner.id,
            plan="full",
        )
        db.session.add(tenant)
        db.session.flush()
        owner.tenant_id = tenant.id
        survey = EncEncuesta(
            tenant_id=tenant.id,
            slug="government-survey-evidence",
            titulo="Consulta territorial",
            estado="publicada",
            tipo="opinion",
        )
        db.session.add(survey)
        db.session.flush()
        db.session.add_all(
            [
                EncRespuesta(
                    encuesta_id=survey.id,
                    tenant_id=tenant.id,
                    canal="web",
                    barrio="Centro",
                    lat=-33.136,
                    lng=-68.49,
                    response_origin="real",
                    submitted_at=datetime.now(timezone.utc),
                ),
                EncRespuesta(
                    encuesta_id=survey.id,
                    tenant_id=tenant.id,
                    canal="web",
                    barrio="Otra jurisdicción",
                    lat=-34.5889,
                    lng=-60.9462,
                    response_origin="real",
                    submitted_at=datetime.now(timezone.utc),
                ),
            ]
        )
        db.session.commit()

        payload = get_heatmap(survey.id)

        assert payload["render_contract"]["state"] == "ready"
        assert len(payload["points"]) == 1
        assert len(payload["cells"]) == 1
        point = payload["points"][0]
        assert point["lat"] == -33.136
        assert point["lng"] == -68.49
        assert point["containment_verified"] is True
        assert point["coordinate_jurisdiction_status"] == "within"
        assert "ide.mendoza.gov.ar" in point["source_ref"]
        assert point["snapshot_sha256"] == (
            "9ee2005d4b2afca51e4b487e3b3f31056f567c37e58027c573ea79bed8ddaa16"
        )
        jurisdiction = payload["metadata"]["jurisdiction"]
        assert jurisdiction["state"] == "verified"
        assert jurisdiction["enforced"] is True
        assert jurisdiction["boundary_authority"]["kind"] == "official"
        assert payload["metadata"]["map"]["render_ready"] is True
        assert payload["metadata"]["map"]["available"] is True
        assert payload["metadata"]["map"]["provider_hint"]
        provenance = payload["metadata"]["provenance"]
        assert provenance["input_points"] == 2
        assert provenance["authorized_points"] == 1
        assert provenance["excluded_points"] == 1
        assert provenance["writes_performed"] is False
        assert payload["metadata"]["points_geojson"]["features"][0][
            "properties"
        ]["containment_verified"] is True

        db.session.remove()
        db.drop_all()


def test_government_survey_heatmap_blocks_without_official_boundary():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        owner = User(
            name="Admin sin límite",
            email="heatmap-no-boundary@test.local",
            rol="admin",
            tenant_slug="government-without-boundary",
        )
        owner.set_password("secret123")
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(
            slug="government-without-boundary",
            nombre="Gobierno sin límite oficial",
            tipo="gobierno",
            municipio_id=owner.id,
            plan="full",
        )
        db.session.add(tenant)
        db.session.flush()
        owner.tenant_id = tenant.id
        survey = EncEncuesta(
            tenant_id=tenant.id,
            slug="blocked-government-heatmap",
            titulo="Consulta sin límite",
            estado="publicada",
            tipo="opinion",
        )
        db.session.add(survey)
        db.session.flush()
        db.session.add(
            EncRespuesta(
                encuesta_id=survey.id,
                tenant_id=tenant.id,
                canal="web",
                lat=-33.136,
                lng=-68.49,
                response_origin="real",
                submitted_at=datetime.now(timezone.utc),
            )
        )
        db.session.commit()

        payload = get_heatmap(survey.id, {"allow_synthetic_geo": True})

        assert payload["points"] == []
        assert payload["cells"] == []
        assert payload["render_contract"]["state"] == "blocked"
        assert payload["render_contract"]["empty_reason"] == (
            "official_jurisdiction_boundary_unavailable"
        )
        assert payload["metadata"]["can_render_heatmap"] is False
        assert payload["metadata"]["using_synthetic_points"] is False
        assert payload["metadata"]["jurisdiction"]["state"] == "blocked"
        assert payload["metadata"]["jurisdiction"]["enforced"] is True
        assert payload["metadata"]["map"]["render_ready"] is False
        assert payload["metadata"]["map"]["reason_code"] == (
            "official_jurisdiction_boundary_unavailable"
        )
        assert payload["metadata"]["provenance"]["authorized_points"] == 0
        assert payload["metadata"]["provenance"]["excluded_points"] == 1

        db.session.remove()
        db.drop_all()


def test_survey_publication_contract_exposes_public_links_and_live_results():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        owner = User(
            name="Admin Junin",
            email="admin-junin@test.com",
            rol="admin",
            tenant_slug="junin",
        )
        owner.password_hash = "test"
        db.session.add(owner)
        db.session.commit()
        tenant = TenantProfile(
            slug="junin-public-contract",
            nombre="Municipalidad de Junin",
            tipo="municipio",
            municipio_id=owner.id,
        )
        db.session.add(tenant)
        db.session.commit()

        encuesta = EncEncuesta(
            tenant_id=tenant.id,
            slug="votacion-plaza",
            titulo="Votacion Plaza",
            estado="publicada",
            tipo="votacion",
            es_votacion_envivo=True,
            mostrar_resultados_envivo=True,
            requiere_identidad=True,
            anonimo_permitido=False,
        )
        db.session.add(encuesta)
        db.session.commit()
        db.session.add(
            EncLink(
                encuesta_id=encuesta.id,
                slug_publico="votacion-plaza-publica",
                canal="whatsapp",
            )
        )
        db.session.commit()

        payload = _build_survey_publication_contract(encuesta.id)

        assert payload["contract_version"] == "surveys.dashboard_publication.v1"
        assert payload["public_state"] == "published"
        assert payload["tenant_slug"] == "junin-public-contract"
        assert payload["slug_publico"] == "votacion-plaza-publica"
        assert payload["is_live_vote"] is True
        assert payload["live_results_enabled"] is True
        assert payload["requires_identity"] is True
        assert payload["anonymous_allowed"] is False
        assert payload["links"]["public_api_endpoint"] == "/api/v2/public/surveys/votacion-plaza-publica?tenant_slug=junin-public-contract"
        assert payload["links"]["respond_endpoint"] == "/api/v2/public/surveys/votacion-plaza-publica/respond?tenant_slug=junin-public-contract"
        assert payload["links"]["live_results_endpoint"] == "/api/v2/public/surveys/votacion-plaza-publica/live-results?tenant_slug=junin-public-contract"
        assert payload["links"]["legacy_live_results_endpoint"] == "/api/public/encuestas/v1/votacion-plaza-publica/live-results"
        assert payload["links"]["qr_endpoint"] == "/api/public/encuestas/v1/votacion-plaza-publica/qr?size=320"
        assert payload["links"]["whatsapp_share_url"].startswith("https://wa.me/?text=")
        assert "open_live_results" in [action["id"] for action in payload["actions"]]

        db.session.remove()
        db.drop_all()
