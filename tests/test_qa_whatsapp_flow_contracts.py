from flask import Flask

from scripts.qa_whatsapp_flows import (
    _finance_webview_or_activation_pending,
    _has_any_marker,
    _route_prefix_registered,
)


def test_finance_probe_reports_activation_pending_without_registered_webview_route():
    app = Flask(__name__)

    probe = _finance_webview_or_activation_pending(app)

    assert probe["ok"] is True
    assert probe["ready"] is False
    assert probe["activation_contract"]["contract_version"] == "finance.activation_plan.v1"
    assert probe["activation_contract"]["state"] == "activation_pending"
    assert probe["activation_contract"]["reason_code"] == "finance_webviews_not_registered"


def test_finance_probe_accepts_registered_finance_webview_route():
    app = Flask(__name__)

    @app.get("/finanzas/<tenant_slug>/alta/<operation_code>")
    def finance_onboarding(tenant_slug, operation_code):
        return {"tenant_slug": tenant_slug, "operation_code": operation_code}

    probe = _finance_webview_or_activation_pending(app)

    assert _route_prefix_registered(app, "/finanzas") is True
    assert probe["ok"] is True
    assert "activation_contract" not in probe


def test_required_link_marker_detection_is_scenario_local():
    bodies = "Tu reclamo esta listo: https://www.chatboc.ar/tracking/claim/123?pin=456"

    assert _has_any_marker(bodies, ["/tracking/claim/", "/api/public/tracking/experience"]) == [
        "/tracking/claim/"
    ]
    assert _has_any_marker("sin link", ["/checkout/", "/catalogo/"]) == []
