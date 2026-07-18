import json
import os
import unittest
from datetime import datetime, timedelta

import jwt

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import TenantProfile, User
from services.twilio_tech_provider import STATE_KEY


class ProductFlowWhatsappConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False
    TWILIO_TECH_PROVIDER_LIVE_ENABLED = False


class ProductFlowWhatsappOnboardingTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(ProductFlowWhatsappConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

        self.admin = User(
            name="Product Flow",
            email="product-flow@test.com",
            rol="admin",
            tenant_slug="product-flow",
            tipo_chat="pyme",
        )
        self.admin.set_password("secret123")
        db.session.add(self.admin)
        db.session.flush()

        self.tenant = TenantProfile(
            slug="product-flow",
            nombre="Product Flow",
            tipo="pyme",
            pyme_id=self.admin.id,
            plan="full",
        )
        db.session.add(self.tenant)
        db.session.commit()
        self.admin.tenant_id = self.tenant.id
        db.session.add(self.admin)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _auth(self):
        token = jwt.encode(
            {
                "user_id": self.admin.id,
                "rol": self.admin.rol,
                "tenant_slug": self.tenant.slug,
                "exp": datetime.utcnow() + timedelta(hours=1),
            },
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        return {"Authorization": f"Bearer {token}", "X-Tenant-Slug": self.tenant.slug}

    def _signup_endpoint(self):
        return (
            f"/api/v2/tenants/{self.tenant.slug}/whatsapp/tech-provider/embedded-signup"
        )

    def test_provider_status_smoke_uses_readiness_checks(self):
        contract = self.client.get(
            f"/api/v2/tenants/{self.tenant.slug}/whatsapp/tech-provider",
            headers=self._auth(),
        )
        self.assertEqual(contract.status_code, 200, contract.get_json())
        contract_payload = contract.get_json()
        self.assertEqual(contract_payload["contract_version"], "twilio.tech_provider.v1")
        self.assertEqual(contract_payload["provider"], "twilio_tech_provider")
        self.assertFalse(contract_payload["automation"]["customer_sees_twilio_console"])
        self.assertFalse(contract_payload["frontend_contract"]["show_manual_console_steps"])
        self.assertTrue(contract_payload["smoke_playbook"]["safe_by_default"])
        completion_verification = contract_payload["embedded_signup"][
            "completion_verification"
        ]
        self.assertEqual(
            completion_verification["contract_version"],
            "twilio.tech_provider.embedded_signup_verification.v1",
        )
        self.assertTrue(completion_verification["validate_only_supported"])
        self.assertFalse(completion_verification["remote_attestation_performed"])
        self.assertFalse(completion_verification["provider_calls_performed"])
        self.assertFalse(completion_verification["authorization_code_persisted"])
        self.assertEqual(
            completion_verification["required_identifiers_by_event"]["FINISH"],
            ["waba_id", "phone_number_id"],
        )

        status = self.client.get(
            f"/api/v2/tenants/{self.tenant.slug}/integrations/whatsapp/status",
            headers=self._auth(),
        )
        self.assertEqual(status.status_code, 200, status.get_json())
        status_payload = status.get_json()
        self.assertEqual(status_payload["contract_version"], "provider.platform_status.v1")
        self.assertEqual(status_payload["frontend_contract"]["render_as"], "whatsapp_provider_status")
        readiness_checks = status_payload["readiness_checks"]
        self.assertGreater(len(readiness_checks), 0)
        self.assertIn("platform_credentials", {item["id"] for item in readiness_checks})

        smoke = self.client.post(
            f"/api/v2/tenants/{self.tenant.slug}/whatsapp/tech-provider/smoke-test/provider_status",
            json={},
            headers=self._auth(),
        )
        self.assertEqual(smoke.status_code, 200, smoke.get_json())
        smoke_payload = smoke.get_json()
        self.assertEqual(smoke_payload["contract_version"], "twilio.tech_provider.smoke_execution.v1")
        self.assertEqual(smoke_payload["details"]["checks_total"], len(readiness_checks))
        self.assertGreater(smoke_payload["details"]["checks_total"], 0)
        self.assertEqual(
            smoke_payload["details"]["readiness_check_ids"],
            [item["id"] for item in readiness_checks if item.get("id")],
        )

    def test_embedded_signup_validate_only_accepts_official_event_without_writes(self):
        response = self.client.post(
            self._signup_endpoint(),
            headers=self._auth(),
            json={
                "validate_only": True,
                "sessionInfo": {
                    "type": "WA_EMBEDDED_SIGNUP",
                    "event": "FINISH",
                    "data": {
                        "waba_id": "123456789",
                        "phone_number_id": "987654321",
                    },
                },
                "authResponse": {"code": "short-lived-meta-code"},
            },
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        verification = payload["verification"]
        self.assertEqual(payload["status"], "validated")
        self.assertFalse(payload["persisted"])
        self.assertTrue(payload["state_unchanged"])
        self.assertFalse(payload["provider_calls_performed"])
        self.assertTrue(verification["accepted"])
        self.assertEqual(verification["payload_shape"], "session_info_envelope")
        self.assertEqual(verification["completion_mode"], "phone_number")
        self.assertEqual(verification["normalized"]["waba_id"], "123456789")
        self.assertEqual(verification["normalized"]["phone_number_id"], "987654321")
        self.assertTrue(verification["security"]["authorization_code_received"])
        self.assertFalse(verification["security"]["authorization_code_persisted"])
        self.assertNotIn("short-lived-meta-code", json.dumps(payload, sort_keys=True))

        db.session.expire_all()
        refreshed = db.session.get(TenantProfile, self.tenant.id)
        self.assertNotIn(STATE_KEY, refreshed.configuracion or {})

    def test_embedded_signup_rejects_incomplete_or_cancelled_events_without_writes(self):
        incomplete = self.client.post(
            self._signup_endpoint(),
            headers=self._auth(),
            json={},
        )

        self.assertEqual(incomplete.status_code, 422, incomplete.get_json())
        incomplete_payload = incomplete.get_json()
        self.assertFalse(incomplete_payload["ok"])
        self.assertFalse(incomplete_payload["retryable"])
        self.assertTrue(incomplete_payload["retryable_after_correction"])
        self.assertTrue(incomplete_payload["state_unchanged"])
        self.assertIn(
            "waba_id_missing",
            incomplete_payload["verification"]["blockers"],
        )

        legacy_without_phone = self.client.post(
            self._signup_endpoint(),
            headers=self._auth(),
            json={"waba_id": "123456789"},
        )
        self.assertEqual(
            legacy_without_phone.status_code,
            422,
            legacy_without_phone.get_json(),
        )
        self.assertIn(
            "phone_number_id_missing",
            legacy_without_phone.get_json()["verification"]["blockers"],
        )

        cancelled = self.client.post(
            self._signup_endpoint(),
            headers=self._auth(),
            json={
                "type": "WA_EMBEDDED_SIGNUP",
                "event": "CANCEL",
                "data": {"waba_id": "123456789"},
            },
        )

        self.assertEqual(cancelled.status_code, 422, cancelled.get_json())
        self.assertEqual(cancelled.get_json()["verification"]["status"], "cancelled")
        self.assertIn(
            "embedded_signup_cancelled",
            cancelled.get_json()["verification"]["blockers"],
        )
        db.session.expire_all()
        refreshed = db.session.get(TenantProfile, self.tenant.id)
        self.assertNotIn(STATE_KEY, refreshed.configuracion or {})

    def test_embedded_signup_only_waba_is_persisted_and_consistent_for_meta_operations(self):
        response = self.client.post(
            self._signup_endpoint(),
            headers=self._auth(),
            json={
                "type": "WA_EMBEDDED_SIGNUP",
                "event": "FINISH_ONLY_WABA",
                "data": {"waba_id": "123456789"},
                "authResponse": {"code": "never-persist-this-code"},
            },
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["persisted"])
        self.assertFalse(payload["provider_calls_performed"])
        self.assertEqual(payload["verification"]["completion_mode"], "only_waba")
        self.assertTrue(payload["state"]["embedded_signup_completion_validated"])
        self.assertEqual(payload["state"]["waba_id"], "123456789")
        self.assertIsNone(payload["state"].get("phone_number_id"))
        self.assertEqual(payload["onboarding"]["status"], "pending_sender_registration")
        self.assertTrue(
            payload["onboarding"]["embedded_signup"]["completion_validated"]
        )
        self.assertEqual(
            payload["onboarding"]["embedded_signup"]["completion_mode"],
            "only_waba",
        )
        self.assertNotIn("never-persist-this-code", json.dumps(payload, sort_keys=True))

        contract_response = self.client.get(
            f"/api/v2/tenants/{self.tenant.slug}/whatsapp/tech-provider",
            headers=self._auth(),
        )
        self.assertEqual(contract_response.status_code, 200, contract_response.get_json())
        contract = contract_response.get_json()
        checklist = {item["id"]: item for item in contract["operator_checklist"]}
        self.assertTrue(checklist["embedded_signup"]["done"])
        self.assertTrue(contract["embedded_signup"]["completion_status"]["accepted"])
        self.assertEqual(
            contract["embedded_signup"]["completion_status"]["mode"],
            "only_waba",
        )

        experience_response = self.client.get(
            f"/api/v2/tenants/{self.tenant.slug}/whatsapp/experience",
            headers=self._auth(),
        )
        self.assertEqual(experience_response.status_code, 200, experience_response.get_json())
        meta_platform = experience_response.get_json()["meta_platform"]
        self.assertTrue(meta_platform["configured"])
        self.assertEqual(meta_platform["status"], "sender_pending")

        conflict = self.client.post(
            self._signup_endpoint(),
            headers=self._auth(),
            json={
                "validate_only": True,
                "type": "WA_EMBEDDED_SIGNUP",
                "event": "FINISH_ONLY_WABA",
                "data": {"waba_id": "222222222"},
            },
        )
        self.assertEqual(conflict.status_code, 422, conflict.get_json())
        self.assertIn(
            "embedded_signup_waba_conflict",
            conflict.get_json()["verification"]["blockers"],
        )
        db.session.expire_all()
        refreshed = db.session.get(TenantProfile, self.tenant.id)
        state_blob = json.dumps(refreshed.configuracion or {}, sort_keys=True)
        self.assertIn("123456789", state_blob)
        self.assertNotIn("222222222", state_blob)
        self.assertNotIn("never-persist-this-code", state_blob)
