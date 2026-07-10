from types import SimpleNamespace

from services.catalog_share import build_catalog_share_payload


def test_catalog_share_payload_uses_canonical_marketplace_url(app, monkeypatch):
    monkeypatch.setitem(app.config, "APP_BASE_URL", "https://www.chatboc.ar")
    monkeypatch.setitem(app.config, "API_BASE_URL", "https://api.chatboc.ar")
    monkeypatch.setattr("services.catalog_share.tiene_archivo_catalogo", lambda _user_id: False)

    owner = SimpleNamespace(id=123, tenant_slug="junin", nombre_empresa="Municipalidad de Junin")

    with app.test_request_context("/"):
        payload = build_catalog_share_payload(owner, channel="whatsapp")

    share = payload["data"]["catalog_share"]
    assert share["view_url"] == "https://www.chatboc.ar/t/junin/market"
    assert "Ver online: https://www.chatboc.ar/t/junin/market" in payload["message_body"]
    assert "/junin/catalogo" not in payload["message_body"]
    assert payload["options_list"][0]["url"] == "https://www.chatboc.ar/t/junin/market"
