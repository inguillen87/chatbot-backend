from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import jwt

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import (
    EncEncuesta,
    EncRespuesta,
    SurveyResponseEffect,
    TenantProfile,
    User,
)
from services.survey_response_effects import (
    STATUS_PENDING,
    STATUS_RETRY_WAIT,
    STATUS_SUCCEEDED,
    stage_survey_response_effects,
)


class SurveyResponseEffectRoutesConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


class V2SurveyResponseEffectRoutesTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(SurveyResponseEffectRoutesConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

        self.admin_1 = self._user("effects-admin-a@test.com", "admin", "effects-a")
        self.admin_2 = self._user("effects-admin-b@test.com", "admin", "effects-b")
        self.viewer = self._user("effects-viewer@test.com", "usuario", "effects-a")
        db.session.flush()

        self.tenant_1 = TenantProfile(
            slug="effects-a",
            nombre="Effects A",
            tipo="municipio",
            municipio_id=self.admin_1.id,
            plan="full",
        )
        self.tenant_2 = TenantProfile(
            slug="effects-b",
            nombre="Effects B",
            tipo="pyme",
            pyme_id=self.admin_2.id,
            plan="full",
        )
        db.session.add_all([self.tenant_1, self.tenant_2])
        db.session.flush()
        self.admin_1.tenant_id = self.tenant_1.id
        self.admin_2.tenant_id = self.tenant_2.id
        self.viewer.tenant_id = self.tenant_1.id
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _user(self, email: str, role: str, tenant_slug: str) -> User:
        user = User(
            name=email.split("@", 1)[0],
            email=email,
            rol=role,
            tenant_slug=tenant_slug,
        )
        user.set_password("secret123")
        db.session.add(user)
        return user

    def _auth(self, user: User, tenant: TenantProfile) -> dict[str, str]:
        token = jwt.encode(
            {
                "user_id": user.id,
                "rol": user.rol,
                "tenant_slug": user.tenant_slug,
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        return {
            "Authorization": f"Bearer {token}",
            "X-Tenant-Slug": tenant.slug,
        }

    def _stage_analytics_effect(
        self,
        tenant: TenantProfile,
        owner: User,
        suffix: str,
    ) -> SurveyResponseEffect:
        survey = EncEncuesta(
            tenant_id=tenant.id,
            slug=f"effects-survey-{suffix}",
            titulo=f"Effects survey {suffix}",
            estado="publicada",
            created_by=owner.id,
            mostrar_resultados_envivo=False,
            puntos_recompensa=0,
        )
        db.session.add(survey)
        db.session.flush()
        response = EncRespuesta(
            encuesta_id=survey.id,
            tenant_id=tenant.id,
            canal="portal",
        )
        db.session.add(response)
        db.session.flush()
        staged = stage_survey_response_effects(
            survey,
            response,
            slug_publico=survey.slug,
            grant_reward=False,
            emit_realtime_update=False,
        )
        db.session.commit()
        self.assertEqual(len(staged), 1)
        return staged[0]

    def test_endpoints_require_auth_role_and_explicit_tenant(self):
        unauthenticated = self.client.get(
            "/api/v2/response-effects",
            headers={"X-Tenant-Slug": self.tenant_1.slug},
        )
        self.assertEqual(unauthenticated.status_code, 401)

        wrong_role = self.client.get(
            "/api/v2/response-effects",
            headers=self._auth(self.viewer, self.tenant_1),
        )
        self.assertEqual(wrong_role.status_code, 403)

        missing_tenant_headers = self._auth(self.admin_1, self.tenant_1)
        missing_tenant_headers.pop("X-Tenant-Slug")
        missing_tenant = self.client.get(
            "/api/v2/response-effects",
            headers=missing_tenant_headers,
        )
        self.assertEqual(missing_tenant.status_code, 400)
        self.assertEqual(missing_tenant.get_json()["reason_code"], "missing_tenant")

    def test_summary_is_tenant_isolated_and_operator_safe(self):
        self._stage_analytics_effect(self.tenant_1, self.admin_1, "summary-a")
        self._stage_analytics_effect(self.tenant_2, self.admin_2, "summary-b")

        own = self.client.get(
            "/api/v2/response-effects",
            headers=self._auth(self.admin_1, self.tenant_1),
        )
        self.assertEqual(own.status_code, 200, own.get_json())
        payload = own.get_json()
        self.assertEqual(
            payload["contract_version"],
            "surveys.response_effects.admin_summary.v1",
        )
        self.assertEqual(payload["tenant"], {"id": self.tenant_1.id, "slug": self.tenant_1.slug})
        self.assertEqual(payload["summary"]["total"], 1)
        self.assertEqual(payload["summary"]["by_status"][STATUS_PENDING], 1)

        serialized = json.dumps(payload, sort_keys=True)
        for forbidden in (
            self.admin_1.email,
            "payload_json",
            "effect_key",
            "response_id",
            "submission_id",
            "last_error",
        ):
            self.assertNotIn(forbidden, serialized)

        cross_tenant = self.client.get(
            "/api/v2/response-effects",
            headers=self._auth(self.admin_1, self.tenant_2),
        )
        self.assertEqual(cross_tenant.status_code, 403, cross_tenant.get_json())
        self.assertEqual(cross_tenant.get_json()["reason_code"], "forbidden_tenant")

    def test_reconcile_passes_only_resolved_tenant_and_bounded_limit(self):
        summary = {
            "contract_version": "surveys.response_effect_summary.v1",
            "tenant_id": self.tenant_1.id,
            "total": 0,
            "due": 0,
            "in_flight": 0,
            "dead": 0,
            "oldest_due_available_at": None,
            "by_status": {},
            "by_effect_type": {},
        }
        dispatch_result = {
            "contract_version": "surveys.response_effect.v1",
            "claimed": 0,
            "processed": 0,
            "succeeded": 0,
            "skipped": 0,
            "retry_wait": 0,
            "dead": 0,
            "fenced": 0,
        }
        headers = self._auth(self.admin_1, self.tenant_1)
        with (
            patch(
                "routes.v2.surveys.dispatch_survey_response_effects",
                return_value=dispatch_result,
            ) as dispatch,
            patch(
                "routes.v2.surveys.summarize_survey_response_effects",
                return_value=summary,
            ),
        ):
            response = self.client.post(
                "/api/v2/response-effects/reconcile",
                json={"limit": 7},
                headers=headers,
            )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(
            payload["contract_version"],
            "surveys.response_effects.reconcile.v1",
        )
        self.assertEqual(payload["limit"], {"applied": 7, "maximum": 100})
        self.assertEqual(payload["reconciliation"], dispatch_result)
        dispatch.assert_called_once_with(tenant_id=self.tenant_1.id, limit=7)

        with patch("routes.v2.surveys.dispatch_survey_response_effects") as blocked:
            excessive = self.client.post(
                "/api/v2/response-effects/reconcile",
                json={"limit": 101},
                headers=headers,
            )
        self.assertEqual(excessive.status_code, 400, excessive.get_json())
        self.assertEqual(
            excessive.get_json()["reason_code"],
            "invalid_response_effect_reconcile_limit",
        )
        blocked.assert_not_called()

    def test_reconcile_recovers_due_retry_without_touching_other_tenant(self):
        own_effect = self._stage_analytics_effect(
            self.tenant_1,
            self.admin_1,
            "recover-a",
        )
        other_effect = self._stage_analytics_effect(
            self.tenant_2,
            self.admin_2,
            "recover-b",
        )
        own_effect.status = STATUS_RETRY_WAIT
        own_effect.attempt_count = 1
        own_effect.last_error = "RuntimeError"
        own_effect.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.session.commit()

        def confirmed_event(**kwargs):
            return SimpleNamespace(
                id=kwargs["event_id"],
                tenant_id=kwargs["tenant_id"],
                event_name=kwargs["event_name"],
                entity_ref=kwargs["entity_ref"],
                metadata_payload=kwargs["payload"],
            )

        with patch(
            "services.analytics.ingestor.analytics_ingestor.track",
            side_effect=confirmed_event,
        ):
            response = self.client.post(
                "/api/v2/response-effects/reconcile",
                json={"limit": 10},
                headers=self._auth(self.admin_1, self.tenant_1),
            )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["reconciliation"]["claimed"], 1)
        self.assertEqual(payload["reconciliation"]["succeeded"], 1)
        self.assertEqual(payload["summary"]["due"], 0)
        self.assertEqual(payload["summary"]["by_status"][STATUS_SUCCEEDED], 1)

        db.session.refresh(own_effect)
        db.session.refresh(other_effect)
        self.assertEqual(own_effect.status, STATUS_SUCCEEDED)
        self.assertEqual(own_effect.attempt_count, 2)
        self.assertEqual(other_effect.status, STATUS_PENDING)
        self.assertEqual(other_effect.attempt_count, 0)


if __name__ == "__main__":
    unittest.main()
