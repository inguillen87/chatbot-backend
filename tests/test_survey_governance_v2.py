from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import unicodedata
import unittest
from unittest.mock import patch

import jwt


os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import AuditEvent, EncEncuesta, EncLink, EncRespuesta, TenantProfile, User
from models_survey_governance import SurveyGovernanceRelease


class SurveyGovernanceTestConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = False
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


class SurveyGovernanceV2Test(unittest.TestCase):
    def setUp(self):
        self.app = create_app(SurveyGovernanceTestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()
        self.owner_1, self.tenant_1 = self._tenant("governance-a")
        self.owner_2, self.tenant_2 = self._tenant("governance-b")

    def tearDown(self):
        db.session.rollback()
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _tenant(self, slug: str):
        owner = User(
            name=f"Owner {slug}",
            email=f"{slug}@chatboc.test",
            rol="admin",
            tenant_slug=slug,
        )
        owner.set_password("secret123")
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(
            slug=slug,
            nombre=slug,
            tipo="municipio",
            pyme_id=owner.id,
            plan="full",
        )
        db.session.add(tenant)
        db.session.flush()
        owner.tenant_id = tenant.id
        db.session.commit()
        return owner, tenant

    def _headers(self, owner: User, tenant: TenantProfile, *, key: str | None = None):
        token = jwt.encode(
            {
                "user_id": owner.id,
                "rol": owner.rol,
                "tenant_slug": tenant.slug,
                "tenant_id": tenant.id,
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Tenant-Slug": tenant.slug,
        }
        if key:
            headers["Idempotency-Key"] = key
        return headers

    @staticmethod
    def _survey_payload():
        now = datetime.now(timezone.utc)
        return {
            "title": "Consulta gobernada sobre espacios públicos",
            "description": "Instrumento de participación con revisión humana",
            "live_vote": True,
            "show_live_results": True,
            "opens_at": (now - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
            "closes_at": (now + timedelta(days=7)).isoformat().replace("+00:00", "Z"),
            "questions": [
                {
                    "type": "single",
                    "label": "¿Qué alternativa preferís?",
                    "required": True,
                    "options": ["Alternativa A", "Alternativa B"],
                    "order_index": 1,
                }
            ],
        }

    @staticmethod
    def _release_payload(
        public_text: str = (
            "Autorizo el tratamiento de mi respuesta para esta consulta institucional."
        ),
    ):
        normalized_text = unicodedata.normalize(
            "NFC", public_text.replace("\r\n", "\n").replace("\r", "\n")
        ).strip(" \n")
        return {
            "eligibility_policy": {
                "policy_version": "eligibility-2026.1",
                "mode": "self_attested",
                "declarations": ["resident_attested", "age_requirement_attested"],
                "human_review_required": True,
                "automated_decision": False,
            },
            "consent_policy": {
                "policy_version": "consent-2026.1",
                "public_text": public_text,
                "text_sha256": hashlib.sha256(
                    normalized_text.encode("utf-8")
                ).hexdigest(),
                "required": True,
            },
            "decision_rules": {
                "quorum": {"type": "minimum_responses", "value": 10},
                "tie": {"procedure": "human_review"},
                "challenge": {
                    "enabled": True,
                    "window_hours": 72,
                    "procedure": "human_review",
                },
                "human_review_required": True,
                "declarative_only": True,
            },
        }

    def _create_survey(self):
        response = self.client.post(
            "/api/v2/surveys",
            json=self._survey_payload(),
            headers=self._headers(self.owner_1, self.tenant_1),
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()["id"]

    def _create_release(self, survey_id: int, *, key: str = "release:create:0001"):
        response = self.client.post(
            f"/api/v2/surveys/{survey_id}/releases",
            json=self._release_payload(),
            headers=self._headers(self.owner_1, self.tenant_1, key=key),
        )
        self.assertIn(response.status_code, {200, 201}, response.get_json())
        return response

    def _create_restricted_release(
        self,
        survey_id: int,
        *,
        key: str = "release:restricted:create:0001",
        mode: str = "manual_review",
    ):
        payload = self._release_payload()
        payload["eligibility_policy"]["mode"] = mode
        response = self.client.post(
            f"/api/v2/surveys/{survey_id}/releases",
            json=payload,
            headers=self._headers(self.owner_1, self.tenant_1, key=key),
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        return response

    def _publish_release(self, survey_id: int, release_id: int, snapshot_hash: str):
        response = self.client.post(
            f"/api/v2/surveys/{survey_id}/releases/{release_id}/publish",
            json={"expected_snapshot_sha256": snapshot_hash},
            headers=self._headers(
                self.owner_1, self.tenant_1, key="release:publish:0001"
            ),
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        return response

    def test_governed_release_response_pinning_replay_close_and_immutability(self):
        survey_id = self._create_survey()
        created = self._create_release(survey_id)
        self.assertEqual(created.status_code, 201)
        release_payload = created.get_json()
        release_id = release_payload["release_id"]
        snapshot_hash = release_payload["snapshot_sha256"]
        self.assertFalse(
            release_payload["assurance"]["regulated_election_certified"]
        )

        replay = self._create_release(survey_id)
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.get_json()["idempotency"]["replayed"])
        conflicting = self._release_payload()
        conflicting["decision_rules"]["quorum"]["value"] = 11
        conflict = self.client.post(
            f"/api/v2/surveys/{survey_id}/releases",
            json=conflicting,
            headers=self._headers(
                self.owner_1, self.tenant_1, key="release:create:0001"
            ),
        )
        self.assertEqual(conflict.status_code, 409, conflict.get_json())

        legacy_publish = self.client.post(
            f"/api/v2/surveys/{survey_id}/publish",
            headers=self._headers(self.owner_1, self.tenant_1),
        )
        self.assertEqual(legacy_publish.status_code, 409, legacy_publish.get_json())

        self._publish_release(survey_id, release_id, snapshot_hash)
        link = EncLink.query.filter_by(encuesta_id=survey_id).one()
        public = self.client.get(f"/api/v2/public/surveys/{link.slug_publico}")
        self.assertEqual(public.status_code, 200, public.get_json())
        public_payload = public.get_json()
        governance = public_payload["governance"]
        self.assertEqual(governance["mode"], "governed_release")
        self.assertEqual(governance["active_release"]["release_id"], release_id)
        consent = governance["active_release"]["governance"]["consent"]
        self.assertEqual(
            consent["public_text"],
            "Autorizo el tratamiento de mi respuesta para esta consulta institucional.",
        )
        self.assertEqual(consent["content_format"], "plain_text")
        self.assertEqual(consent["normalization"], "unicode_nfc_lf_trim_v1")
        self.assertTrue(consent["stores_public_text"])
        self.assertFalse(consent["records_participant_input"])
        self.assertTrue(
            governance["active_release"]["completeness"]["public_consent"][
                "complete"
            ]
        )
        self.assertFalse(governance["result_certified"])

        question = public_payload["preguntas"][0]
        answer = {
            "submission_id": "governed:response:0001",
            "instrument_revision": public_payload["instrument_revision"],
            "respuestas": [
                {
                    "pregunta_id": question["id"],
                    "opcion_id": question["opciones"][0]["id"],
                }
            ],
        }
        missing_ack = self.client.post(
            f"/api/v2/public/surveys/{link.slug_publico}/respond",
            json=answer,
            headers={"Idempotency-Key": answer["submission_id"]},
        )
        self.assertEqual(missing_ack.status_code, 409, missing_ack.get_json())
        self.assertEqual(EncRespuesta.query.count(), 0)

        answer["governance"] = {
            "release_id": release_id,
            "snapshot_sha256": snapshot_hash,
            "eligibility_policy_version": "eligibility-2026.1",
            "consent_policy_version": "consent-2026.1",
            "eligibility_acknowledged": True,
            "consent_accepted": True,
        }
        accepted = self.client.post(
            f"/api/public/encuestas/v1/{link.slug_publico}/responder",
            json=answer,
            headers={"Idempotency-Key": answer["submission_id"]},
        )
        self.assertEqual(accepted.status_code, 201, accepted.get_json())
        accepted_payload = accepted.get_json()
        self.assertEqual(accepted_payload["governance"]["release_id"], release_id)
        self.assertEqual(
            accepted_payload["idempotency"]["governance"]["release_id"],
            release_id,
        )
        saved = EncRespuesta.query.one()
        self.assertEqual(saved.governance_release_id, release_id)
        self.assertEqual(
            saved.governance_eligibility_policy_version, "eligibility-2026.1"
        )

        replay_response = self.client.post(
            f"/api/pwa/public/surveys/{link.slug_publico}/respond?tenant={self.tenant_1.slug}",
            json=answer,
            headers={"Idempotency-Key": answer["submission_id"]},
        )
        self.assertEqual(replay_response.status_code, 200, replay_response.get_json())
        self.assertTrue(replay_response.get_json()["replayed"])
        self.assertEqual(EncRespuesta.query.count(), 1)

        immutable_edit = self.client.patch(
            f"/api/v2/surveys/{survey_id}",
            json={"title": "Intento de cambio"},
            headers=self._headers(self.owner_1, self.tenant_1),
        )
        self.assertEqual(immutable_edit.status_code, 409, immutable_edit.get_json())

        closed = self.client.post(
            f"/api/v2/surveys/{survey_id}/releases/{release_id}/close",
            json={"human_review_reference": "review:HumanClosure0001"},
            headers=self._headers(
                self.owner_1, self.tenant_1, key="release:close:0001"
            ),
        )
        self.assertEqual(closed.status_code, 200, closed.get_json())
        closure = closed.get_json()["closure"]
        self.assertEqual(closure["manifest"]["response_count"], 1)
        self.assertFalse(
            closure["manifest"]["assurance"]["result_certified"]
        )

        from services.survey_governance import survey_governance_contract

        closed_contract = survey_governance_contract(
            db.session.get(EncEncuesta, survey_id)
        )
        self.assertIsNone(closed_contract["active_release"])
        self.assertEqual(closed_contract["latest_release"]["status"], "closed")
        self.assertFalse(closed_contract["accepting_responses"])

        release = db.session.get(SurveyGovernanceRelease, release_id)
        release.snapshot_sha256 = "f" * 64
        with self.assertRaises(ValueError):
            db.session.flush()
        db.session.rollback()
        self.assertEqual(
            AuditEvent.query.filter_by(
                tenant_id=self.tenant_1.id,
                resource_type="survey_governance_release",
            ).count(),
            3,
        )

    def test_cross_tenant_and_unpinned_response_fail_closed(self):
        survey_id = self._create_survey()
        created = self._create_release(survey_id)
        release_id = created.get_json()["release_id"]
        self._publish_release(
            survey_id, release_id, created.get_json()["snapshot_sha256"]
        )

        foreign_list = self.client.get(
            f"/api/v2/surveys/{survey_id}/releases",
            headers=self._headers(self.owner_2, self.tenant_2),
        )
        self.assertEqual(foreign_list.status_code, 404, foreign_list.get_json())
        foreign_close = self.client.post(
            f"/api/v2/surveys/{survey_id}/releases/{release_id}/close",
            json={"human_review_reference": "review:ForeignHuman0001"},
            headers=self._headers(
                self.owner_2, self.tenant_2, key="release:foreign:close:0001"
            ),
        )
        self.assertEqual(foreign_close.status_code, 404, foreign_close.get_json())

        db.session.add(
            EncRespuesta(
                encuesta_id=survey_id,
                tenant_id=self.tenant_1.id,
                canal="legacy_test",
                privacy_mode="legacy",
            )
        )
        db.session.commit()
        close = self.client.post(
            f"/api/v2/surveys/{survey_id}/releases/{release_id}/close",
            json={"human_review_reference": "review:HumanClosure0002"},
            headers=self._headers(
                self.owner_1, self.tenant_1, key="release:close:0002"
            ),
        )
        self.assertEqual(close.status_code, 409, close.get_json())
        self.assertEqual(
            close.get_json()["reason_code"], "survey_governance_unpinned_responses"
        )
        self.assertEqual(
            db.session.get(SurveyGovernanceRelease, release_id).status, "published"
        )

    def test_release_list_exposes_explicit_fail_closed_action_authority(self):
        survey_id = self._create_survey()

        initial = self.client.get(
            f"/api/v2/surveys/{survey_id}/releases",
            headers=self._headers(self.owner_1, self.tenant_1),
        )
        self.assertEqual(initial.status_code, 200, initial.get_json())
        initial_payload = initial.get_json()
        self.assertEqual(
            initial_payload["tenant"],
            {"id": self.tenant_1.id, "slug": self.tenant_1.slug},
        )
        self.assertEqual(
            initial_payload["capabilities"],
            {
                "read": True,
                "manage": True,
                "plan_allows_write": True,
                "create_release": True,
                "required_for_mutation": "survey.governance.manage",
            },
        )
        self.assertIsNone(initial_payload["active_release_id"])
        self.assertIsNone(initial_payload["latest_release_id"])

        created = self._create_release(survey_id)
        release_id = created.get_json()["release_id"]
        draft_list = self.client.get(
            f"/api/v2/surveys/{survey_id}/releases",
            headers=self._headers(self.owner_1, self.tenant_1),
        ).get_json()
        self.assertFalse(draft_list["capabilities"]["create_release"])
        self.assertIsNone(draft_list["active_release_id"])
        self.assertEqual(draft_list["latest_release_id"], release_id)
        self.assertEqual(
            draft_list["items"][0]["capabilities"],
            {"can_publish": True, "can_close": False},
        )

        self._publish_release(
            survey_id, release_id, created.get_json()["snapshot_sha256"]
        )
        published_list = self.client.get(
            f"/api/v2/surveys/{survey_id}/releases",
            headers=self._headers(self.owner_1, self.tenant_1),
        ).get_json()
        self.assertEqual(published_list["active_release_id"], release_id)
        self.assertEqual(published_list["latest_release_id"], release_id)
        self.assertEqual(
            published_list["items"][0]["capabilities"],
            {"can_publish": False, "can_close": True},
        )

        closed = self.client.post(
            f"/api/v2/surveys/{survey_id}/releases/{release_id}/close",
            json={"human_review_reference": "review:AuthorityTest0001"},
            headers=self._headers(
                self.owner_1, self.tenant_1, key="release:authority:close:0001"
            ),
        )
        self.assertEqual(closed.status_code, 200, closed.get_json())
        closed_list = self.client.get(
            f"/api/v2/surveys/{survey_id}/releases",
            headers=self._headers(self.owner_1, self.tenant_1),
        ).get_json()
        self.assertIsNone(closed_list["active_release_id"])
        self.assertEqual(closed_list["latest_release_id"], release_id)
        self.assertEqual(
            closed_list["items"][0]["capabilities"],
            {"can_publish": False, "can_close": False},
        )

    def test_atomic_audit_rollback_and_sensitive_policy_rejection(self):
        survey_id = self._create_survey()
        original_start = db.session.get(EncEncuesta, survey_id).inicio_at
        invalid = self._release_payload()
        invalid["eligibility_policy"]["padron"] = ["person-1"]
        rejected = self.client.post(
            f"/api/v2/surveys/{survey_id}/releases",
            json=invalid,
            headers=self._headers(
                self.owner_1, self.tenant_1, key="release:invalid:0001"
            ),
        )
        self.assertEqual(rejected.status_code, 422, rejected.get_json())
        self.assertEqual(SurveyGovernanceRelease.query.count(), 0)

        with patch.object(db.session, "commit", side_effect=RuntimeError("audit db failed")):
            failed = self.client.post(
                f"/api/v2/surveys/{survey_id}/releases",
                json=self._release_payload(),
                headers=self._headers(
                    self.owner_1, self.tenant_1, key="release:atomic:0001"
                ),
            )
        self.assertEqual(failed.status_code, 500, failed.get_json())
        self.assertEqual(SurveyGovernanceRelease.query.count(), 0)
        self.assertEqual(
            AuditEvent.query.filter_by(
                tenant_id=self.tenant_1.id,
                resource_type="survey_governance_release",
            ).count(),
            0,
        )
        self.assertEqual(db.session.get(EncEncuesta, survey_id).inicio_at, original_start)

    def test_publish_maps_missing_identity_hmac_preflight_to_stable_json(self):
        survey_id = self._create_survey()
        survey = db.session.get(EncEncuesta, survey_id)
        survey.privacy_mode = "source_anonymous"
        survey.privacy_policy_version = "privacy-2026.1"
        survey.privacy_policy_url = "https://example.test/privacidad"
        survey.privacy_consent_required = True
        survey.response_retention_days = 365
        survey.puntos_recompensa = 0
        survey.politica_unicidad = "anon_id"
        db.session.commit()

        created = self._create_release(
            survey_id,
            key="release:hmac-missing:create:0001",
        )
        release_id = created.get_json()["release_id"]
        snapshot_sha256 = created.get_json()["snapshot_sha256"]
        audit_count_before = AuditEvent.query.filter_by(
            tenant_id=self.tenant_1.id,
            resource_type="survey_governance_release",
        ).count()
        self.app.config["SURVEY_IDENTITY_HMAC_SECRET_V1"] = ""

        response = self.client.post(
            f"/api/v2/surveys/{survey_id}/releases/{release_id}/publish",
            json={"expected_snapshot_sha256": snapshot_sha256},
            headers=self._headers(
                self.owner_1,
                self.tenant_1,
                key="release:hmac-missing:publish:0001",
            ),
        )

        self.assertEqual(response.status_code, 503, response.get_data(as_text=True))
        self.assertEqual(response.content_type, "application/json")
        payload = response.get_json()
        self.assertEqual(
            set(payload),
            {
                "action_hint",
                "contract_version",
                "error",
                "message",
                "reason_code",
                "request_id",
                "retryable",
                "status_code",
            },
        )
        self.assertEqual(payload["contract_version"], "surveys.privacy.v1")
        self.assertEqual(payload["status_code"], 503)
        self.assertEqual(
            payload["reason_code"],
            "survey_identity_hmac_secret_unavailable",
        )
        self.assertEqual(
            payload["action_hint"],
            "configure_survey_identity_hmac_secret_v1",
        )
        self.assertFalse(payload["retryable"])
        self.assertEqual(
            payload["error"],
            {
                "code": 503,
                "message": "No se puede publicar: falta el secreto HMAC dedicado de encuestas.",
            },
        )
        self.assertEqual(payload["message"], payload["error"]["message"])
        self.assertTrue(payload["request_id"])

        db.session.expire_all()
        blocked_release = db.session.get(SurveyGovernanceRelease, release_id)
        blocked_survey = db.session.get(EncEncuesta, survey_id)
        self.assertEqual(blocked_release.status, "draft")
        self.assertIsNone(blocked_release.published_at)
        self.assertIsNone(blocked_release.published_by_user_id)
        self.assertIsNone(blocked_release.publish_idempotency_key)
        self.assertIsNone(blocked_release.publish_request_hash)
        self.assertEqual(blocked_survey.estado, "borrador")
        self.assertIsNone(EncLink.query.filter_by(encuesta_id=survey_id).first())
        self.assertEqual(
            AuditEvent.query.filter_by(
                tenant_id=self.tenant_1.id,
                resource_type="survey_governance_release",
            ).count(),
            audit_count_before,
        )

    def test_public_consent_normalization_bounds_and_hash_are_fail_closed(self):
        survey_id = self._create_survey()
        cases = []

        missing = self._release_payload()
        missing["consent_policy"].pop("public_text")
        cases.append(
            (missing, "release:consent:missing", "survey_consent_public_text_required")
        )

        mismatch = self._release_payload()
        mismatch["consent_policy"]["text_sha256"] = "0" * 64
        cases.append(
            (mismatch, "release:consent:mismatch", "survey_consent_text_hash_mismatch")
        )

        control = self._release_payload("Texto aprobado\tcon tab")
        cases.append(
            (
                control,
                "release:consent:control",
                "survey_consent_public_text_control_invalid",
            )
        )

        oversized = self._release_payload("a" * 4001)
        cases.append(
            (
                oversized,
                "release:consent:oversized",
                "survey_consent_public_text_length_invalid",
            )
        )

        lone_surrogate = self._release_payload()
        lone_surrogate["consent_policy"]["public_text"] = "texto\ud800"
        cases.append(
            (
                lone_surrogate,
                "release:consent:unicode",
                "survey_consent_public_text_unicode_invalid",
            )
        )

        for payload, key, reason_code in cases:
            with self.subTest(reason_code=reason_code):
                response = self.client.post(
                    f"/api/v2/surveys/{survey_id}/releases",
                    json=payload,
                    headers=self._headers(self.owner_1, self.tenant_1, key=key),
                )
                self.assertEqual(response.status_code, 422, response.get_json())
                self.assertEqual(response.get_json()["reason_code"], reason_code)
                self.assertEqual(SurveyGovernanceRelease.query.count(), 0)

        decomposed = "  Autorizo Cafe\u0301.\r\nSegunda línea. \n"
        normalized = "Autorizo Café.\nSegunda línea."
        accepted = self.client.post(
            f"/api/v2/surveys/{survey_id}/releases",
            json=self._release_payload(decomposed),
            headers=self._headers(
                self.owner_1, self.tenant_1, key="release:consent:normalized"
            ),
        )
        self.assertEqual(accepted.status_code, 201, accepted.get_json())
        consent = accepted.get_json()["governance"]["consent"]
        self.assertEqual(consent["public_text"], normalized)
        self.assertEqual(
            consent["text_sha256"],
            hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        )

    def test_legacy_release_without_public_consent_is_visible_but_blocked(self):
        survey_id = self._create_survey()
        created = self._create_release(survey_id)
        release = db.session.get(
            SurveyGovernanceRelease, created.get_json()["release_id"]
        )
        snapshot = json.loads(release.snapshot_json)
        consent = snapshot["governance"]["consent"]
        consent.pop("public_text")
        consent.pop("content_format")
        consent.pop("normalization")
        release.snapshot_json = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        release.snapshot_sha256 = hashlib.sha256(
            release.snapshot_json.encode("utf-8")
        ).hexdigest()
        release.policy_sha256 = hashlib.sha256(
            json.dumps(
                snapshot["governance"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        db.session.commit()

        listed = self.client.get(
            f"/api/v2/surveys/{survey_id}/releases",
            headers=self._headers(self.owner_1, self.tenant_1),
        ).get_json()
        self.assertFalse(
            listed["items"][0]["completeness"]["public_consent"]["complete"]
        )
        self.assertFalse(listed["items"][0]["capabilities"]["can_publish"])

        publish = self.client.post(
            f"/api/v2/surveys/{survey_id}/releases/{release.id}/publish",
            json={"expected_snapshot_sha256": release.snapshot_sha256},
            headers=self._headers(
                self.owner_1, self.tenant_1, key="release:legacy:publish"
            ),
        )
        self.assertEqual(publish.status_code, 409, publish.get_json())
        self.assertEqual(
            publish.get_json()["reason_code"], "survey_consent_public_text_required"
        )

        owner_id = self.owner_1.id
        now = datetime.now(timezone.utc)
        release.status = "published"
        release.published_at = now
        release.published_by_user_id = owner_id
        release.publish_idempotency_key = "legacy:already:published"
        release.publish_request_hash = "f" * 64
        survey = db.session.get(EncEncuesta, survey_id)
        survey.estado = "publicada"
        db.session.add(
            EncLink(encuesta_id=survey.id, slug_publico=survey.slug, canal="web")
        )
        db.session.commit()

        from services.survey_governance import (
            SurveyGovernanceError,
            governed_response_context,
            survey_governance_contract,
        )

        public_contract = survey_governance_contract(survey)
        self.assertFalse(public_contract["accepting_responses"])
        self.assertEqual(
            public_contract["blocked_reason_code"],
            "survey_consent_public_text_required",
        )
        self.assertFalse(
            public_contract["active_release"]["completeness"]["public_consent"][
                "complete"
            ]
        )
        with self.assertRaises(SurveyGovernanceError) as blocked:
            governed_response_context(survey, {"governance": {}})
        self.assertEqual(
            blocked.exception.reason_code, "survey_consent_public_text_required"
        )

    def test_restricted_eligibility_is_gated_issued_and_redeemed_atomically(self):
        from models_survey_eligibility import (
            SurveyEligibilityGrant,
            SurveyEligibilityTerminal,
        )

        survey_id = self._create_survey()
        created = self._create_restricted_release(survey_id)
        release_id = created.get_json()["release_id"]
        snapshot_sha256 = created.get_json()["snapshot_sha256"]

        blocked_publish = self.client.post(
            f"/api/v2/surveys/{survey_id}/releases/{release_id}/publish",
            json={"expected_snapshot_sha256": snapshot_sha256},
            headers=self._headers(
                self.owner_1,
                self.tenant_1,
                key="release:restricted:publish:disabled",
            ),
        )
        self.assertEqual(blocked_publish.status_code, 503, blocked_publish.get_json())
        self.assertEqual(
            blocked_publish.get_json()["reason_code"],
            "survey_eligibility_gate_unavailable",
        )
        self.assertEqual(db.session.get(EncEncuesta, survey_id).estado, "borrador")

        self.app.config.update(
            ENABLE_SURVEY_ELIGIBILITY_GRANTS_V1=True,
            SURVEY_ELIGIBILITY_GRANT_TENANT_IDS=str(self.tenant_1.id),
            SURVEY_ELIGIBILITY_SECRET_V1="eligibility-test-secret-32-bytes-minimum-value",
        )
        published = self.client.post(
            f"/api/v2/surveys/{survey_id}/releases/{release_id}/publish",
            json={"expected_snapshot_sha256": snapshot_sha256},
            headers=self._headers(
                self.owner_1,
                self.tenant_1,
                key="release:restricted:publish:enabled",
            ),
        )
        self.assertEqual(published.status_code, 200, published.get_json())

        subject_ref = "subj_" + ("A" * 43)
        review_reference = "review:case-eligibility-0001"
        issue_headers = self._headers(
            self.owner_1,
            self.tenant_1,
            key="eligibility:issue:0001",
        )
        issued = self.client.post(
            f"/api/v2/surveys/{survey_id}/releases/{release_id}/eligibility-grants",
            json={
                "subject_ref": subject_ref,
                "review_reference": review_reference,
            },
            headers=issue_headers,
        )
        self.assertEqual(issued.status_code, 201, issued.get_json())
        self.assertIn("no-store", issued.headers.get("Cache-Control", ""))
        issued_payload = issued.get_json()
        credential = issued_payload["credential"]
        grant_ref = issued_payload["grant_ref"]
        self.assertEqual(issued_payload["tenant_id"], self.tenant_1.id)
        self.assertEqual(issued_payload["survey_id"], survey_id)
        self.assertEqual(issued_payload["release_id"], release_id)
        self.assertTrue(credential.startswith("sec1_"))
        self.assertEqual(
            issued_payload["assurance"]["assurance_level"],
            "human_reviewed_opaque_grant",
        )
        serialized_issue = json.dumps(issued_payload, sort_keys=True)
        self.assertNotIn(subject_ref, serialized_issue)
        self.assertNotIn(review_reference, serialized_issue)

        replayed_issue = self.client.post(
            f"/api/v2/surveys/{survey_id}/releases/{release_id}/eligibility-grants",
            json={
                "subject_ref": subject_ref,
                "review_reference": review_reference,
            },
            headers=issue_headers,
        )
        self.assertEqual(replayed_issue.status_code, 200, replayed_issue.get_json())
        self.assertEqual(replayed_issue.get_json()["credential"], credential)
        self.assertTrue(replayed_issue.get_json()["idempotency"]["replayed"])
        self.assertEqual(replayed_issue.get_json()["tenant_id"], self.tenant_1.id)
        self.assertEqual(replayed_issue.get_json()["survey_id"], survey_id)
        self.assertEqual(replayed_issue.get_json()["release_id"], release_id)

        cross_tenant_issue = self.client.post(
            f"/api/v2/surveys/{survey_id}/releases/{release_id}/eligibility-grants",
            json={
                "subject_ref": "subj_" + ("X" * 43),
                "review_reference": "review:cross-tenant-0001",
            },
            headers=self._headers(
                self.owner_2,
                self.tenant_2,
                key="eligibility:issue:cross-tenant-0001",
            ),
        )
        self.assertEqual(
            cross_tenant_issue.status_code, 404, cross_tenant_issue.get_json()
        )
        self.assertEqual(
            cross_tenant_issue.get_json()["reason_code"],
            "survey_governance_release_not_found",
        )
        self.assertNotIn("credential", cross_tenant_issue.get_json())

        link = EncLink.query.filter_by(encuesta_id=survey_id).one()
        public = self.client.get(f"/api/v2/public/surveys/{link.slug_publico}")
        self.assertEqual(public.status_code, 200, public.get_json())
        public_payload = public.get_json()
        eligibility = public_payload["governance"]["eligibility"]
        self.assertTrue(eligibility["credential_required"])
        self.assertTrue(eligibility["intake_available"])
        self.assertEqual(eligibility["gate_status"], "ready")
        self.assertEqual(
            eligibility["transport"]["header_name"],
            "X-Survey-Eligibility-Credential",
        )
        self.assertEqual(
            public_payload["frontend_contract"]["eligibility"], eligibility
        )

        question = public_payload["preguntas"][0]
        answer = {
            "submission_id": "restricted:response:0001",
            "instrument_revision": public_payload["instrument_revision"],
            "anon_id": "restricted-anon-0001",
            "respuestas": [
                {
                    "pregunta_id": question["id"],
                    "opcion_id": question["opciones"][0]["id"],
                }
            ],
            "governance": {
                "release_id": release_id,
                "snapshot_sha256": snapshot_sha256,
                "eligibility_policy_version": "eligibility-2026.1",
                "consent_policy_version": "consent-2026.1",
                "eligibility_acknowledged": True,
                "consent_accepted": True,
            },
        }
        submission_headers = {
            "Idempotency-Key": answer["submission_id"],
            "X-Anon-Id": answer["anon_id"],
        }
        missing = self.client.post(
            f"/api/v2/public/surveys/{link.slug_publico}/respond",
            json=answer,
            headers=submission_headers,
        )
        self.assertEqual(missing.status_code, 428, missing.get_json())
        self.assertEqual(
            missing.get_json()["reason_code"],
            "survey_eligibility_credential_required",
        )
        self.assertEqual(EncRespuesta.query.count(), 0)
        self.assertEqual(SurveyEligibilityTerminal.query.count(), 0)

        smuggled = dict(answer)
        smuggled["eligibility_credential"] = credential
        smuggled["submission_id"] = "restricted:response:smuggled"
        smuggled_response = self.client.post(
            f"/api/v2/public/surveys/{link.slug_publico}/respond",
            json=smuggled,
            headers={"Idempotency-Key": smuggled["submission_id"]},
        )
        self.assertEqual(smuggled_response.status_code, 400, smuggled_response.get_json())
        self.assertEqual(
            smuggled_response.get_json()["reason_code"],
            "survey_eligibility_credential_transport_invalid",
        )
        self.assertNotIn(credential, json.dumps(smuggled_response.get_json()))

        accepted = self.client.post(
            f"/api/v2/public/surveys/{link.slug_publico}/respond",
            json=answer,
            headers={
                **submission_headers,
                "X-Survey-Eligibility-Credential": credential,
            },
        )
        self.assertEqual(accepted.status_code, 201, accepted.get_json())
        accepted_payload = accepted.get_json()
        accepted_eligibility = accepted_payload["governance"]["eligibility"]
        self.assertEqual(
            accepted_eligibility["decision"],
            "verified_by_opaque_grant",
        )
        redemption = accepted_eligibility["redemption"]
        self.assertEqual(redemption["state"], "committed")
        self.assertTrue(redemption["persisted"])
        self.assertEqual(redemption["response_id"], accepted_payload["response_id"])
        serialized_ack = json.dumps(accepted_payload, sort_keys=True)
        self.assertNotIn(credential, serialized_ack)
        self.assertNotIn(grant_ref, serialized_ack)
        self.assertNotIn(subject_ref, serialized_ack)
        self.assertEqual(EncRespuesta.query.count(), 1)
        self.assertEqual(SurveyEligibilityTerminal.query.count(), 1)

        grant = SurveyEligibilityGrant.query.one()
        self.assertNotEqual(grant.credential_digest, credential)
        self.assertFalse(
            any(
                value == credential
                for value in grant.__dict__.values()
                if isinstance(value, str)
            )
        )

        replay = self.client.post(
            f"/api/v2/public/surveys/{link.slug_publico}/respond",
            json=answer,
            headers=submission_headers,
        )
        self.assertEqual(replay.status_code, 200, replay.get_json())
        self.assertTrue(replay.get_json()["replayed"])
        self.assertEqual(replay.get_json()["response_id"], accepted_payload["response_id"])

        second = dict(answer)
        second["submission_id"] = "restricted:response:0002"
        second["anon_id"] = "restricted-anon-0002"
        consumed = self.client.post(
            f"/api/v2/public/surveys/{link.slug_publico}/respond",
            json=second,
            headers={
                "Idempotency-Key": second["submission_id"],
                "X-Anon-Id": second["anon_id"],
                "X-Survey-Eligibility-Credential": credential,
            },
        )
        self.assertEqual(consumed.status_code, 409, consumed.get_json())
        self.assertEqual(
            consumed.get_json()["reason_code"],
            "survey_eligibility_credential_consumed",
        )
        self.assertEqual(EncRespuesta.query.count(), 1)
        self.assertEqual(SurveyEligibilityTerminal.query.count(), 1)

        second_issue = self.client.post(
            f"/api/v2/surveys/{survey_id}/releases/{release_id}/eligibility-grants",
            json={
                "subject_ref": "subj_" + ("B" * 43),
                "review_reference": "review:case-eligibility-0002",
            },
            headers=self._headers(
                self.owner_1,
                self.tenant_1,
                key="eligibility:issue:0002",
            ),
        )
        self.assertEqual(second_issue.status_code, 201, second_issue.get_json())
        second_credential = second_issue.get_json()["credential"]
        second_grant_ref = second_issue.get_json()["grant_ref"]
        revoke_headers = self._headers(
            self.owner_1,
            self.tenant_1,
            key="eligibility:revoke:0002",
        )
        revoked = self.client.post(
            f"/api/v2/surveys/{survey_id}/releases/{release_id}/eligibility-grants/{second_grant_ref}/revoke",
            json={"reason_code": "administrative_revocation"},
            headers=revoke_headers,
        )
        self.assertEqual(revoked.status_code, 200, revoked.get_json())
        self.assertIn("no-store", revoked.headers.get("Cache-Control", ""))
        self.assertEqual(revoked.get_json()["state"], "revoked")
        self.assertEqual(revoked.get_json()["tenant_id"], self.tenant_1.id)
        self.assertEqual(revoked.get_json()["survey_id"], survey_id)
        self.assertEqual(revoked.get_json()["release_id"], release_id)
        revoked_replay = self.client.post(
            f"/api/v2/surveys/{survey_id}/releases/{release_id}/eligibility-grants/{second_grant_ref}/revoke",
            json={"reason_code": "administrative_revocation"},
            headers=revoke_headers,
        )
        self.assertEqual(revoked_replay.status_code, 200, revoked_replay.get_json())
        self.assertTrue(revoked_replay.get_json()["idempotency"]["replayed"])
        self.assertEqual(revoked_replay.get_json()["tenant_id"], self.tenant_1.id)
        self.assertEqual(revoked_replay.get_json()["survey_id"], survey_id)
        self.assertEqual(revoked_replay.get_json()["release_id"], release_id)

        cross_tenant_revoke = self.client.post(
            f"/api/v2/surveys/{survey_id}/releases/{release_id}/eligibility-grants/{second_grant_ref}/revoke",
            json={"reason_code": "administrative_revocation"},
            headers=self._headers(
                self.owner_2,
                self.tenant_2,
                key="eligibility:revoke:cross-tenant-0001",
            ),
        )
        self.assertEqual(
            cross_tenant_revoke.status_code, 404, cross_tenant_revoke.get_json()
        )
        self.assertEqual(
            cross_tenant_revoke.get_json()["reason_code"],
            "survey_governance_release_not_found",
        )

        revoked_answer = dict(answer)
        revoked_answer["submission_id"] = "restricted:response:revoked"
        revoked_answer["anon_id"] = "restricted-anon-revoked"
        rejected_revoked = self.client.post(
            f"/api/v2/public/surveys/{link.slug_publico}/respond",
            json=revoked_answer,
            headers={
                "Idempotency-Key": revoked_answer["submission_id"],
                "X-Anon-Id": revoked_answer["anon_id"],
                "X-Survey-Eligibility-Credential": second_credential,
            },
        )
        self.assertEqual(rejected_revoked.status_code, 403, rejected_revoked.get_json())
        self.assertEqual(
            rejected_revoked.get_json()["reason_code"],
            "survey_eligibility_grant_revoked",
        )
        self.assertEqual(EncRespuesta.query.count(), 1)

        summary = self.client.get(
            f"/api/v2/surveys/{survey_id}/releases/{release_id}/eligibility-summary",
            headers=self._headers(self.owner_1, self.tenant_1),
        )
        self.assertEqual(summary.status_code, 200, summary.get_json())
        self.assertEqual(summary.get_json()["counts"]["issued"], 2)
        self.assertEqual(summary.get_json()["counts"]["redeemed"], 1)
        self.assertEqual(summary.get_json()["counts"]["revoked"], 1)
        self.assertIsNone(summary.get_json()["eligible_population"])
        self.assertIsNone(summary.get_json()["participation_rate"])
        self.assertIsNone(summary.get_json()["abstentions"])

        employee = User(
            name="Eligibility operator",
            email="eligibility-operator@chatboc.test",
            rol="empleado",
            es_empleado=True,
            tenant_id=self.tenant_1.id,
            tenant_slug=self.tenant_1.slug,
            accesibilidad={"employee_scope": {"capabilities": []}},
        )
        employee.set_password("secret123")
        db.session.add(employee)
        db.session.commit()
        denied = self.client.get(
            f"/api/v2/surveys/{survey_id}/releases/{release_id}/eligibility-summary",
            headers=self._headers(employee, self.tenant_1),
        )
        self.assertEqual(denied.status_code, 403, denied.get_json())
        self.assertEqual(
            denied.get_json()["reason_code"],
            "survey_eligibility_manage_capability_required",
        )
        employee.accesibilidad = {
            "employee_scope": {
                "capabilities": ["survey.eligibility.manage"],
            }
        }
        db.session.add(employee)
        db.session.commit()
        permitted = self.client.get(
            f"/api/v2/surveys/{survey_id}/releases/{release_id}/eligibility-summary",
            headers=self._headers(employee, self.tenant_1),
        )
        self.assertEqual(permitted.status_code, 200, permitted.get_json())

        cross_tenant = self.client.get(
            f"/api/v2/surveys/{survey_id}/releases/{release_id}/eligibility-summary",
            headers=self._headers(self.owner_2, self.tenant_2),
        )
        self.assertEqual(cross_tenant.status_code, 404, cross_tenant.get_json())
        self.assertEqual(
            cross_tenant.get_json()["reason_code"],
            "survey_governance_release_not_found",
        )

        forged = EncRespuesta(
            encuesta_id=survey_id,
            tenant_id=self.tenant_1.id,
            huella_unica="forged-eligibility-mirror-only",
            submitted_at=datetime.now(timezone.utc),
            content_hash="d" * 64,
            privacy_mode="legacy",
            governance_release_id=release_id,
            governance_eligibility_policy_version="eligibility-2026.1",
            governance_consent_policy_version="consent-2026.1",
            governance_acknowledged_at=datetime.now(timezone.utc),
            eligibility_contract_version="surveys.public_eligibility.v1",
            eligibility_decision="verified_by_opaque_grant",
            eligibility_verified_at=datetime.now(timezone.utc),
        )
        db.session.add(forged)
        db.session.commit()
        self.assertIsNone(
            SurveyEligibilityTerminal.query.filter_by(response_id=forged.id).first()
        )
        unsafe_close = self.client.post(
            f"/api/v2/surveys/{survey_id}/releases/{release_id}/close",
            json={"human_review_reference": "review:EligibilityLedger0001"},
            headers=self._headers(
                self.owner_1,
                self.tenant_1,
                key="release:restricted:close:forged-ledger",
            ),
        )
        self.assertEqual(unsafe_close.status_code, 409, unsafe_close.get_json())
        self.assertEqual(
            unsafe_close.get_json()["reason_code"],
            "survey_governance_eligibility_receipt_incomplete",
        )


if __name__ == "__main__":
    unittest.main()
