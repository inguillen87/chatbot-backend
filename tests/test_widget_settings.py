import json
import unittest
from unittest.mock import patch

try:
    from app import create_app
    from config import Config
    from models import TenantProfile, User, WidgetSettings, db
    from utils.auth_helpers import generar_token
except Exception:
    create_app = None


class _TestConfig(Config):
    TESTING = True
    SESSION_TYPE = "filesystem"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"


def _create_tenant_with_owner():
    owner = User(
        name="Admin",
        email="admin@example.com",
        password_hash="hash",
        rol="admin",
    )
    db.session.add(owner)
    db.session.flush()
    owner.token = generar_token(owner.id, owner.rol, None, None, None)

    tenant = TenantProfile(
        slug="tenant-demo",
        nombre="Demo Municipio",
        tipo="municipio",
        municipio=owner,
    )
    db.session.add(tenant)
    db.session.commit()
    return owner, tenant


@unittest.skipIf(create_app is None, "Flask not available")
class WidgetSettingsTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.owner, self.tenant = _create_tenant_with_owner()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_widget_settings_defaults_and_embed(self):
        resp = self.client.get(
            "/widget-settings",
            headers={"Authorization": self.owner.token},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("embed_code", data)
        self.assertIn("data-singleton", data["embed_code"])
        self.assertIn(self.tenant.slug, data["embed_code"])

    def test_widget_settings_update_and_widget_config_merge(self):
        payload = {
            "primary_color": "#ff0000",
            "secondary_color": "#00ff00",
            "avatar_url": "http://example.com/avatar.png",
            "font_family": "Inter",
            "bubble_shape": "square",
            "default_open": True,
        }
        put = self.client.put(
            "/widget-settings",
            json=payload,
            headers={"Authorization": self.owner.token},
        )
        self.assertEqual(put.status_code, 200)

        widget_resp = self.client.get(
            f"/api/public/widget-config?tenant={self.tenant.slug}",
        )
        self.assertEqual(widget_resp.status_code, 200)
        widget_data = widget_resp.get_json()
        attrs = widget_data["widget"]["attributes"]
        self.assertEqual(attrs["data-primary-color"], payload["primary_color"])
        self.assertEqual(attrs["data-accent-color"], payload["secondary_color"])
        self.assertEqual(attrs["data-logo-url"], payload["avatar_url"])
        self.assertEqual(attrs["data-font-family"], payload["font_family"])
        self.assertEqual(attrs["data-bubble-shape"], payload["bubble_shape"])
        self.assertEqual(attrs["data-default-open"], "true")
        self.assertIn("data-widget-preset", attrs)
        self.assertIn("data-motion-level", attrs)
        self.assertIn("data-glassmorphism", attrs)
        self.assertIn("data-logo-ring", attrs)
        self.assertIn("data-typing-animation", attrs)
        self.assertIn("data-bubble-animation", attrs)
        self.assertIn("data-launcher-animation", attrs)
        self.assertIn("data-message-enter-animation", attrs)
        self.assertIn("data-logo-badge-style", attrs)
        self.assertIn("data-cursor-trail", attrs)
        self.assertIn("data-ambient-particles", attrs)
        self.assertIn("ux", widget_data["builder_config"])
        self.assertIn("support_channels", widget_data["widget"])
        self.assertIn("enterprise_iteration", widget_data["builder_config"])
        self.assertIn("live_chat", widget_data["widget"]["support_channels"])
        self.assertIn("whatsapp", widget_data["widget"]["support_channels"])
        self.assertIn("voice_call", widget_data["widget"]["support_channels"])
        self.assertIn("video_call", widget_data["widget"]["support_channels"])
        self.assertIn("data-realtime-model", attrs)
        self.assertIn("data-realtime-voice-enabled", attrs)
        self.assertIn("data-realtime-video-enabled", attrs)
        self.assertIn("data-avatar-enabled", attrs)
        self.assertIn("data-avatar-type", attrs)
        self.assertIn("data-avatar-persona", attrs)
        self.assertEqual(attrs["data-avatar-contract-version"], "chatboc.avatar.v1")
        self.assertEqual(attrs["data-avatar-type"], "chatboc_bot")
        self.assertEqual(attrs["data-avatar-persona"], "bot_chatboc")
        self.assertEqual(attrs["data-avatar-display-name"], "BOT Chatboc")
        self.assertEqual(widget_data["builder_config"]["avatar"]["type"], "chatboc_bot")
        self.assertEqual(widget_data["widget"]["builder_config"]["enterprise_iteration"]["realtime"]["voice_handoff"]["supports_whatsapp_followup"], True)



    def test_widget_config_defaults_video_realtime_disabled(self):
        widget_resp = self.client.get(
            f"/api/public/widget-config?tenant={self.tenant.slug}",
        )
        self.assertEqual(widget_resp.status_code, 200)
        widget_data = widget_resp.get_json()
        attrs = widget_data["widget"]["attributes"]
        self.assertEqual(attrs.get("data-realtime-video-enabled"), "false")
        self.assertFalse(widget_data["widget"]["support_channels"]["video_call"]["enabled"])

    def test_widget_config_disables_voice_cta_when_realtime_voice_disabled(self):
        self.tenant.configuracion = {"realtime_voice_enabled": False, "widget_tokens": [self.owner.token]}
        db.session.add(self.tenant)
        db.session.commit()

        widget_resp = self.client.get(
            f"/api/public/widget-config?tenant={self.tenant.slug}",
        )

        self.assertEqual(widget_resp.status_code, 200)
        widget_data = widget_resp.get_json()
        self.assertFalse(widget_data["support_channels"]["voice_call"]["enabled"])
        self.assertFalse(widget_data["realtime_voice"]["enabled"])
        self.assertFalse(widget_data["realtime_voice"]["features"]["tool_calling"])

    def test_public_realtime_session_requires_openai_key(self):
        self.app.config["OPENAI_API_KEY"] = ""
        response = self.client.post(
            "/api/public/realtime/session",
            json={"tenant_slug": self.tenant.slug, "channel": "voice", "widget_token": self.owner.token},
        )
        self.assertEqual(response.status_code, 503)
        payload = response.get_json()
        self.assertEqual(payload.get("error"), "openai_api_key_missing")


    def test_public_realtime_session_rejects_invalid_widget_token(self):
        response = self.client.post(
            "/api/public/realtime/session",
            json={"tenant_slug": self.tenant.slug, "channel": "voice", "widget_token": "invalid-token"},
        )
        self.assertEqual(response.status_code, 403)
        payload = response.get_json()
        self.assertEqual(payload.get("error"), "widget_token_invalid")

    @patch("routes.public_resolver.urllib_request.urlopen")
    def test_public_realtime_session_accepts_frontend_contract_fields(self, mock_urlopen):
        class MockOpenAIResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps({"id": "sess_123", "client_secret": {"value": "ek_mock"}}).encode("utf-8")

        mock_urlopen.return_value = MockOpenAIResponse()
        self.app.config["OPENAI_API_KEY"] = "mock-key"

        response = self.client.post(
            "/api/public/realtime/session",
            json={
                "tenant_slug": self.tenant.slug,
                "channel": "voice",
                "widget_token": self.owner.token,
                "model": "gpt-4o-realtime-preview",
                "fallback_model": "gpt-realtime",
                "voice": "marin",
                "transport": "webrtc",
                "profile": "realtime_voice_native",
                "active_vertical": "municipio",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["model"], "gpt-realtime")
        self.assertEqual(payload["avatar"]["fallback_model"], "gpt-realtime")
        self.assertEqual(payload["avatar"]["transport"], "webrtc")
        self.assertEqual(payload["avatar"]["active_vertical"], "municipio")
        self.assertEqual(payload["avatar"]["requested_model_ignored"], "gpt-4o-realtime-preview")
        self.assertEqual(payload["avatar"]["openai_realtime_contract"], "client_secrets.v2")
        self.assertEqual(payload["avatar"]["avatar_type"], "chatboc_bot")
        self.assertEqual(payload["avatar"]["avatar_persona"], "bot_chatboc")
        self.assertEqual(payload["avatar_contract"]["contract_version"], "chatboc.avatar.v1")
        self.assertEqual(payload["avatar_contract"]["display_name"], "BOT Chatboc")
        self.assertEqual(payload["avatar"]["translation"]["target_language"], "es")
        self.assertTrue(payload["avatar"]["translation"]["channels"]["realtime_voice_call"])
        request_obj = mock_urlopen.call_args.args[0]
        upstream_payload = json.loads(request_obj.data.decode("utf-8"))
        self.assertEqual(request_obj.full_url, "https://api.openai.com/v1/realtime/client_secrets")
        self.assertEqual(upstream_payload["session"]["type"], "realtime")
        self.assertEqual(upstream_payload["session"]["model"], "gpt-realtime")
        self.assertEqual(upstream_payload["session"]["output_modalities"], ["audio"])
        self.assertEqual(upstream_payload["session"]["audio"]["output"]["voice"], "marin")
        self.assertEqual(upstream_payload["session"]["audio"]["input"]["turn_detection"]["type"], "semantic_vad")
        self.assertEqual(upstream_payload["session"]["tracing"]["metadata"]["openai_endpoint"], "/v1/realtime/client_secrets")
        self.assertIn("ingles", upstream_payload["session"]["instructions"])
        self.assertIn("portugues", upstream_payload["session"]["instructions"])
        tool_names = {tool["name"] for tool in upstream_payload["session"]["tools"]}
        self.assertIn("crear_reclamo", tool_names)
        self.assertIn("capturar_lead_comercial", tool_names)
        self.assertIn("registrar_solicitud_operativa", tool_names)
        self.assertIsNone(request_obj.get_header("Openai-beta"))

    def test_public_realtime_voice_capabilities_expose_bot_avatar_and_business_tools(self):
        response = self.client.get(
            "/api/public/realtime/voice-capabilities",
            query_string={"tenant": self.tenant.slug},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["provider"], "openai_realtime")
        self.assertEqual(payload["api_generation"], "realtime_ga_client_secrets_v2")
        self.assertEqual(payload["avatar"]["contract_version"], "chatboc.avatar.v1")
        self.assertEqual(payload["avatar"]["type"], "chatboc_bot")
        self.assertEqual(payload["avatar"]["persona"], "bot_chatboc")
        municipio_actions = payload["verticals"]["municipio"]["actions"]
        self.assertIn("crear_reclamo", municipio_actions)
        self.assertIn("capturar_lead_comercial", municipio_actions)
        self.assertIn("registrar_solicitud_operativa", municipio_actions)
        self.assertIn("registrar_intencion_pago_colegio", payload["verticals"]["colegio"]["actions"])
        self.assertIn("cotizar_envio", payload["verticals"]["pyme"]["actions"])
        self.assertIn("registrar_solicitud_operativa", payload["verticals"]["general"]["actions"])

    @patch("routes.public_resolver.urllib_request.urlopen")
    def test_public_realtime_session_ignores_legacy_beta_header_env(self, mock_urlopen):
        class MockOpenAIResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps({"id": "sess_123", "client_secret": {"value": "ek_mock"}}).encode("utf-8")

        mock_urlopen.return_value = MockOpenAIResponse()
        self.app.config["OPENAI_API_KEY"] = "mock-key"
        self.app.config["OPENAI_REALTIME_BETA_HEADER"] = "legacy-header-should-be-ignored"

        response = self.client.post(
            "/api/public/realtime/session",
            json={
                "tenant_slug": self.tenant.slug,
                "channel": "voice",
                "widget_token": self.owner.token,
            },
        )

        self.assertEqual(response.status_code, 200)
        request_obj = mock_urlopen.call_args.args[0]
        self.assertIsNone(request_obj.get_header("Openai-beta"))

    def test_public_realtime_action_event_rejects_unknown_action(self):
        response = self.client.post(
            "/api/public/realtime/action-event",
            json={
                "tenant_slug": self.tenant.slug,
                "widget_token": self.owner.token,
                "channel": "voice",
                "action": "hack_system",
            },
        )
        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertEqual(payload.get("error"), "action_not_allowed")

    def test_public_realtime_action_event_requires_action(self):
        response = self.client.post(
            "/api/public/realtime/action-event",
            json={"tenant_slug": self.tenant.slug, "widget_token": self.owner.token, "channel": "voice"},
        )
        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertEqual(payload.get("error"), "action_required")

    def test_public_realtime_session_includes_rate_limit_headers(self):
        self.app.config["OPENAI_API_KEY"] = ""
        response = self.client.post(
            "/api/public/realtime/session",
            json={"tenant_slug": self.tenant.slug, "channel": "voice", "widget_token": self.owner.token},
        )
        self.assertEqual(response.status_code, 503)
        self.assertIn("X-RateLimit-Limit", response.headers)
        self.assertIn("X-RateLimit-Window", response.headers)

    def test_public_widget_config_allows_querystring_tenant_fallback(self):
        resp = self.client.get(
            "/api/public/tenants/perfil/widget-config",
            query_string={"tenant": self.tenant.slug, "tenant_slug": self.tenant.slug},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.get_json(), dict)


if __name__ == "__main__":
    unittest.main()
