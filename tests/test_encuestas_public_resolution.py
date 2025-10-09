from routes import encuestas_public


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
