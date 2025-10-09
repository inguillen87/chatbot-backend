from database import db
from models import EncEncuesta, EncLink
from routes import encuestas_public
from services.encuestas_service import list_public_encuestas_for_tenant


class _DummyRespuesta:
    def __init__(self, respuesta_id):
        self.id = respuesta_id


def test_resolve_tenant_from_domain_map(app):
    app.config["PUBLIC_ENCUESTAS_DOMAIN_MAP"] = {"chatboc.ar": 7}
    app.config["PUBLIC_ENCUESTAS_DEFAULT_TENANT_ID"] = None

    with app.test_request_context(
        "/public/encuestas",
        headers={"Host": "chatboc.ar"},
    ):
        assert encuestas_public._resolve_tenant_from_request() == 7


def test_resolve_tenant_from_forwarded_host(app):
    app.config["PUBLIC_ENCUESTAS_DOMAIN_MAP"] = {"chatboc.ar": 11}
    app.config["PUBLIC_ENCUESTAS_DEFAULT_TENANT_ID"] = None

    with app.test_request_context(
        "/public/encuestas",
        headers={"X-Forwarded-Host": "api.chatboc.ar, www.chatboc.ar:443"},
    ):
        assert encuestas_public._resolve_tenant_from_request() == 11


def test_resolve_tenant_uses_default(app):
    app.config["PUBLIC_ENCUESTAS_DOMAIN_MAP"] = {}
    app.config["PUBLIC_ENCUESTAS_DEFAULT_TENANT_ID"] = 5

    with app.test_request_context("/public/encuestas"):
        assert encuestas_public._resolve_tenant_from_request() == 5


def test_public_urls_use_canonical_base(client, monkeypatch):
    client.application.config["PUBLIC_ENCUESTAS_CANONICAL_BASE_URL"] = "https://www.chatboc.ar"

    def fake_list(_tenant_id, limit):
        assert limit == 5
        return [({"id": 1}, "slug-demo")]

    monkeypatch.setattr(
        "routes.encuestas_public.list_public_encuestas_for_tenant",
        fake_list,
    )
    monkeypatch.setattr(
        "routes.encuestas_public.serialize_public_encuesta",
        lambda encuesta, slug_publico: {"slug": slug_publico},
    )

    response = client.get("/public/encuestas")
    assert response.status_code == 200
    data = response.get_json()
    assert data[0]["url_publica"] == "https://www.chatboc.ar/e/slug-demo"


def test_respuestas_alias_reuses_handler(client, monkeypatch):
    saved_calls = {}

    def fake_save(slug, payload, ctx):
        saved_calls["slug"] = slug
        saved_calls["payload"] = payload
        saved_calls["ctx"] = ctx
        return _DummyRespuesta(123)

    monkeypatch.setattr("routes.encuestas_public.save_respuesta", fake_save)

    response = client.post(
        "/public/encuestas/demo-encuesta/respuestas",
        json={"respuesta": "ok"},
        headers={"X-Forwarded-For": "1.1.1.1"},
    )
    assert response.status_code == 201
    body = response.get_json()
    assert body == {"ok": True, "respuesta_id": 123}
    assert saved_calls["slug"] == "demo-encuesta"
    assert saved_calls["payload"] == {"respuesta": "ok"}
    assert saved_calls["ctx"]["ip"] == "1.1.1.1"


def test_share_redirects_to_canonical(client):
    client.application.config["PUBLIC_ENCUESTAS_CANONICAL_BASE_URL"] = "https://www.chatboc.ar"

    response = client.get("/e/demo-slug")
    assert response.status_code == 302
    assert response.headers["Location"] == "https://www.chatboc.ar/e/demo-slug"


def test_share_returns_payload_without_canonical(client, monkeypatch):
    client.application.config["PUBLIC_ENCUESTAS_CANONICAL_BASE_URL"] = None

    monkeypatch.setattr(
        "routes.encuestas_public.get_public_encuesta",
        lambda slug: {"slug": slug},
    )
    monkeypatch.setattr(
        "routes.encuestas_public.serialize_public_encuesta",
        lambda encuesta, slug_publico: {"slug": slug_publico},
    )

    response = client.get("/e/demo-slug")
    assert response.status_code == 200
    assert response.get_json() == {"slug": "demo-slug"}


def _create_public_encuesta(slug: str, slug_publico: str) -> None:
    encuesta = EncEncuesta(
        tenant_id=4,
        slug=slug,
        titulo="Encuesta Demo",
        descripcion="Demo",
        tipo="opinion",
        estado="publicada",
    )
    link = EncLink(
        encuesta=encuesta,
        slug_publico=slug_publico,
        canal="web",
    )
    db.session.add(encuesta)
    db.session.add(link)
    db.session.commit()


def test_share_endpoint_handles_alias_without_link(client):
    slug = "junin-participa"
    slug_publico = "junin-participa-abcdef"
    _create_public_encuesta(slug, slug_publico)

    response = client.get(f"/e/{slug_publico}")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["slug"] == slug_publico

    link = EncLink.query.filter_by(slug_publico=slug_publico).first()
    db.session.delete(link)
    db.session.commit()

    response = client.get(f"/e/{slug_publico}")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["slug"] == slug_publico

    response = client.get(f"/e/{slug}")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["slug"] == slug


def test_list_public_encuestas_falls_back_to_slug(client):
    with client.application.app_context():
        encuesta = EncEncuesta(
            tenant_id=4,
            slug="encuesta-sin-link",
            titulo="Participá de la consulta",
            descripcion="Validamos fallback sin link",
            tipo="opinion",
            estado="publicada",
        )
        db.session.add(encuesta)
        db.session.commit()

        resultados = list_public_encuestas_for_tenant(encuesta.tenant_id, limit=10)
        assert any(
            item_encuesta.id == encuesta.id and slug == encuesta.slug
            for item_encuesta, slug in resultados
        )
