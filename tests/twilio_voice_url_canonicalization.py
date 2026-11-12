"""Local voice URL regression; no provider or deployed database acceptance.

Run in an isolated process with python -m tests.twilio_voice_url_canonicalization.
"""
from tests.profile_acceptance_runtime import prepare_process, create_disposable_app

if __name__ == "__main__":
    prepare_process()

from contextlib import ExitStack
from copy import deepcopy
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from services import twilio_tech_provider as provider


LEGACY = "https://chatbot-backend-2e14.onrender.com"
CANONICAL = "https://api.chatboc.ar"
CONFIG = {"PUBLIC_API_BASE_URL": CANONICAL,
          "TWILIO_TECH_PROVIDER_LIVE_ENABLED": False,
          "TWILIO_VOICE_PROVISIONING_LIVE_ENABLED": False}


def tenant_with_urls(origin=LEGACY, slug="junin", *, nested=True):
    query = f"tenant={slug}&vertical=municipio&intent=reclamos"
    state = {"voice_url": f"{origin}/twilio/voice?{query}",
             "voice_fallback_url": f"{origin}/voice/fallback?{query}",
             "voice_status_callback_url": f"{origin}/voice/status",
             "voice_vertical": "municipio", "voice_intent": "reclamos",
             "voice_status": "voice_application_plan_ready",
             "voice_twiml_app_sid": "synthetic-existing-app"}
    cfg = {provider.STATE_KEY: state} if nested else state
    return SimpleNamespace(id=22, slug=slug, nombre="Synthetic municipality",
                           tipo="municipio", vertical="municipio",
                           configuracion=cfg, whatsapp_sender_id=None)


class VoiceUrlCanonicalizationTests(unittest.TestCase):
    def test_persisted_legacy_urls_rebase_without_changing_state_or_readiness(self):
        for nested in (True, False):
            with self.subTest(nested=nested):
                tenant = tenant_with_urls(nested=nested)
                before = deepcopy(tenant.configuracion)
                result = provider.build_twilio_tech_provider_contract(tenant, CONFIG)
                query = "tenant=junin&vertical=municipio&intent=reclamos"
                self.assertEqual(result["voice"]["voice_url"], f"{CANONICAL}/twilio/voice?{query}")
                self.assertEqual(result["voice"]["fallback_url"], f"{CANONICAL}/voice/fallback?{query}")
                self.assertEqual(result["voice"]["status_callback_url"], f"{CANONICAL}/voice/status")
                self.assertEqual(result["voice"]["twiml_app_sid"], "synthetic-existing-app")
                if nested:
                    self.assertEqual(result["voice"]["status"], "voice_application_plan_ready")
                self.assertFalse(result["automation"]["live_enabled"])
                self.assertEqual(tenant.configuracion, before)

    def test_explicit_legacy_request_preserves_valid_scope_query(self):
        tenant = tenant_with_urls()
        state = tenant.configuracion[provider.STATE_KEY]
        payload = {key: state[key] for key in
                   ("voice_url", "voice_fallback_url", "voice_status_callback_url")}
        before = deepcopy(payload)
        result = provider.build_voice_application_request(tenant, payload, CONFIG)
        for key in payload:
            self.assertEqual(result[key], payload[key].replace(LEGACY, CANONICAL, 1))
        self.assertEqual(result["tenant_slug"], "junin")
        self.assertEqual(result["vertical"], "municipio")
        self.assertEqual(result["intent"], "reclamos")
        self.assertEqual(payload, before)

    def test_custom_and_canonical_urls_are_preserved_exactly(self):
        for origin in ("https://voice.customer.invalid", CANONICAL,
                       "https://other-customer.onrender.com"):
            with self.subTest(origin=origin):
                tenant = tenant_with_urls(origin)
                state = tenant.configuracion[provider.STATE_KEY]
                contract = provider.build_twilio_tech_provider_contract(tenant, CONFIG)
                self.assertEqual(contract["voice"]["voice_url"], state["voice_url"])
                self.assertEqual(contract["voice"]["fallback_url"], state["voice_fallback_url"])
                self.assertEqual(contract["voice"]["status_callback_url"], state["voice_status_callback_url"])
                request = provider.build_voice_application_request(tenant, state, CONFIG)
                for key in ("voice_url", "voice_fallback_url", "voice_status_callback_url"):
                    self.assertEqual(request[key], state[key])

    def test_lookalike_host_credentials_and_other_paths_are_not_reinterpreted(self):
        candidates = [
            "https://chatbot-backend-2e14.onrender.com.customer.invalid/twilio/voice?tenant=junin",
            "https://user@chatbot-backend-2e14.onrender.com/twilio/voice?tenant=junin",
            f"{LEGACY}:8443/twilio/voice?tenant=junin",
            f"{LEGACY}/twilio/voice/inbound?tenant=junin",
            f"{LEGACY}/twilio/voice/?tenant=junin",
        ]
        for url in candidates:
            with self.subTest(url=url):
                tenant = tenant_with_urls()
                tenant.configuracion[provider.STATE_KEY]["voice_url"] = url
                result = provider.build_twilio_tech_provider_contract(tenant, CONFIG)
                self.assertEqual(result["voice"]["voice_url"], url)

    def test_legacy_foreign_duplicate_or_missing_scope_uses_canonical_tenant_default(self):
        queries = ["tenant=foreign&vertical=municipio&intent=reclamos",
                   "tenant=junin&tenant=foreign", "tenant=junin&tenant_slug=foreign",
                   "tenant=junin&vertical=municipio&vertical=ventas", "intent=reclamos",
                   "tenant=junin&intent=bad%26tenant%3Dforeign", "tenant=junin&next=foreign",
                   "tenant=junin#unexpected", "tenant=junin&vertical=municipio&sector=ventas"]
        for query in queries:
            with self.subTest(query=query):
                tenant = tenant_with_urls()
                tenant.configuracion[provider.STATE_KEY]["voice_url"] = f"{LEGACY}/twilio/voice?{query}"
                result = provider.build_twilio_tech_provider_contract(tenant, CONFIG)
                self.assertEqual(result["voice"]["voice_url"], f"{CANONICAL}/twilio/voice?tenant=junin")

    def test_legacy_request_cannot_rebind_the_canonical_tenant(self):
        tenant = tenant_with_urls()
        result = provider.build_voice_application_request(tenant, {
            "tenant_slug": "foreign",
            "voice_url": f"{LEGACY}/twilio/voice?tenant=foreign&vertical=municipio&intent=reclamos",
        }, CONFIG)
        self.assertEqual(result["tenant_slug"], "junin")
        self.assertEqual(result["voice_url"], f"{CANONICAL}/twilio/voice?tenant=junin&vertical=municipio&intent=reclamos")

    def test_valid_encoded_query_is_preserved_and_untrusted_base_is_not_used(self):
        tenant = tenant_with_urls()
        url = f"{LEGACY}/twilio/voice?tenant=%6Aunin&vertical=municipio&intent=reclamos"
        tenant.configuracion[provider.STATE_KEY]["voice_url"] = url
        result = provider.build_twilio_tech_provider_contract(tenant, CONFIG)
        self.assertEqual(result["voice"]["voice_url"], url.replace(LEGACY, CANONICAL, 1))
        for base in ("https://user:credential@api.chatboc.ar", "https://api.chatboc.ar?tenant=foreign"):
            with self.subTest(base=base):
                result = provider.build_twilio_tech_provider_contract(tenant, {**CONFIG, "PUBLIC_API_BASE_URL": base})
                self.assertEqual(result["voice"]["voice_url"], url)

    def test_dry_run_keeps_provider_calls_disabled_and_does_not_mutate_tenant(self):
        tenant = tenant_with_urls()
        before = deepcopy(tenant.configuracion)
        with ExitStack() as stack:
            guards = [stack.enter_context(patch.object(provider, name,
                       side_effect=AssertionError("No provider calls"))) for name in
                      ("_twilio_post_form", "_twilio_post_json", "_twilio_get_json")]
            result = provider.provision_twilio_voice_application(tenant,
                     {"voice_url": before[provider.STATE_KEY]["voice_url"]}, CONFIG)
            for guard in guards:
                guard.assert_not_called()
        self.assertEqual(result["mode"], "dry_run")
        self.assertEqual(result["state_patch"]["voice_status"], "voice_application_plan_ready")
        self.assertTrue(all(step["status"] in ("planned", "pending_sender") for step in result["steps"]))
        self.assertEqual(tenant.configuracion, before)


class VoiceContractHttpReadOnlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="chatboc-voice-url-local-")
        cls.app, cls.accounts, cls.password = create_disposable_app(cls.directory.name)
        cls.app.config.update(CONFIG)
        from database import db
        from models import TenantProfile
        with cls.app.app_context():
            tenant = db.session.get(TenantProfile, cls.accounts["acceptance-a"]["tenant_id"])
            tenant.plan = "full"
            tenant.configuracion = tenant_with_urls(slug="acceptance-a").configuracion
            db.session.commit()

    @classmethod
    def tearDownClass(cls):
        from database import db
        with cls.app.app_context():
            db.session.remove()
            db.engine.dispose()
        cls.directory.cleanup()

    def test_authenticated_get_rebases_persisted_urls_without_any_dml_or_provider_call(self):
        from database import db
        from models import TenantProfile
        from sqlalchemy import event
        client = self.app.test_client(use_cookies=False)
        login = client.post("/auth/login", json={
            "email": self.accounts["acceptance-a"]["email"], "password": self.password})
        self.assertEqual(login.status_code, 200)
        token = login.get_json()["token"]
        writes = []
        def record_write(_conn, _cursor, statement, _parameters, _context, _executemany):
            if statement.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
                writes.append(statement.split(None, 1)[0])
        with self.app.app_context():
            tenant_id = self.accounts["acceptance-a"]["tenant_id"]
            before = deepcopy(db.session.get(TenantProfile, tenant_id).configuracion)
            db.session.remove()
            engine = db.engine
            event.listen(engine, "before_cursor_execute", record_write)
            try:
                with ExitStack() as stack:
                    guards = [stack.enter_context(patch.object(provider, name,
                               side_effect=AssertionError("No provider calls"))) for name in
                              ("_twilio_post_form", "_twilio_post_json", "_twilio_get_json")]
                    response = client.get("/api/v2/tenants/acceptance-a/whatsapp/tech-provider",
                                          headers={"Authorization": f"Bearer {token}"})
                    for guard in guards:
                        guard.assert_not_called()
            finally:
                event.remove(engine, "before_cursor_execute", record_write)
            self.assertEqual(response.status_code, 200)
            body = response.get_json()
            query = "tenant=acceptance-a&vertical=municipio&intent=reclamos"
            self.assertEqual(body["voice"]["voice_url"], f"{CANONICAL}/twilio/voice?{query}")
            self.assertEqual(body["voice"]["status"], "voice_application_plan_ready")
            self.assertFalse(body["automation"]["live_enabled"])
            self.assertEqual(writes, [], "Contract GET must not write any table")
            self.assertEqual(db.session.get(TenantProfile, tenant_id).configuracion, before)


if __name__ == "__main__":
    unittest.main()
