from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import unittest


os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import AuditEvent, EncEncuesta, EncRespuesta, TenantProfile, User
from models_survey_eligibility import (
    SURVEY_ELIGIBILITY_REDEMPTION_CONTRACT_VERSION,
    SurveyEligibilityGrant,
    SurveyEligibilityTerminal,
)
from models_survey_governance import (
    SURVEY_RELEASE_CONTRACT_VERSION,
    SurveyGovernanceRelease,
)
from services.survey_eligibility import (
    ELIGIBILITY_DECISION_VERIFIED,
    SURVEY_ELIGIBILITY_CREDENTIAL_HEADER,
    SurveyEligibilityError,
    assert_eligibility_grant_active,
    eligibility_aggregate,
    issue_eligibility_grant,
    lock_eligibility_grant_for_submission,
    public_eligibility_contract,
    resolve_eligibility_gate,
    response_eligibility_contract,
    revoke_eligibility_grant,
    serialize_issue_receipt,
    serialize_revocation_receipt,
    stage_eligibility_redemption,
)


class SurveyEligibilityTestConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = False
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


class SurveyEligibilityServiceTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(SurveyEligibilityTestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.owner = User(
            name="Eligibility Owner",
            email="eligibility-owner@chatboc.test",
            rol="admin",
            tenant_slug="eligibility-tenant",
        )
        self.owner.set_password("secret123")
        db.session.add(self.owner)
        db.session.flush()
        self.tenant = TenantProfile(
            slug="eligibility-tenant",
            nombre="Eligibility tenant",
            tipo="municipio",
            pyme_id=self.owner.id,
            plan="full",
        )
        db.session.add(self.tenant)
        db.session.flush()
        self.owner.tenant_id = self.tenant.id
        db.session.commit()
        self.gate_config = {
            "ENABLE_SURVEY_ELIGIBILITY_GRANTS_V1": True,
            "SURVEY_ELIGIBILITY_GRANT_TENANT_IDS": str(self.tenant.id),
            "SURVEY_ELIGIBILITY_SECRET_V1": "eligibility-test-secret-32-bytes-minimum-value",
        }
        self.survey, self.release = self._published_release(
            slug="restricted-one", mode="institution_attested"
        )

    def tearDown(self):
        db.session.rollback()
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    @staticmethod
    def _opaque_subject(seed: str) -> str:
        digest = hashlib.sha256(seed.encode("utf-8")).digest()
        import base64

        return "subj_" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def _published_release(
        self,
        *,
        slug: str,
        mode: str,
        ends_in: timedelta = timedelta(days=7),
    ):
        survey = EncEncuesta(
            tenant_id=self.tenant.id,
            slug=slug,
            titulo=f"Survey {slug}",
            estado="publicada",
            created_by=self.owner.id,
        )
        # The intentionally explicit model values keep this fixture independent
        # from route defaults while satisfying the governed-response contract.
        survey.privacy_mode = "legacy"
        survey.privacy_consent_required = False
        db.session.add(survey)
        db.session.flush()
        policy_version = f"elig-{slug}-v1"
        snapshot = {
            "collection_rules": {
                "ends_at": (datetime.now(timezone.utc) + ends_in).isoformat()
            },
            "governance": {
                "eligibility": {
                    "contract_version": "surveys.eligibility_policy.v1",
                    "policy_version": policy_version,
                    "mode": mode,
                    "declarations": [],
                    "human_review_required": True,
                    "automated_decision": False,
                    "stores_roster_or_pii": False,
                    "decision_state": "not_evaluated",
                }
            },
        }
        snapshot_json = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        release = SurveyGovernanceRelease(
            tenant_id=self.tenant.id,
            survey_id=survey.id,
            version_number=1,
            status="published",
            contract_version=SURVEY_RELEASE_CONTRACT_VERSION,
            snapshot_json=snapshot_json,
            snapshot_sha256=hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest(),
            policy_sha256="a" * 64,
            eligibility_policy_version=policy_version,
            consent_policy_version=f"consent-{slug}-v1",
            created_by_user_id=self.owner.id,
            create_idempotency_key=f"release:create:{slug}",
            create_request_hash="b" * 64,
            published_by_user_id=self.owner.id,
            published_at=datetime.now(timezone.utc),
            publish_idempotency_key=f"release:publish:{slug}",
            publish_request_hash="c" * 64,
        )
        db.session.add(release)
        db.session.commit()
        return survey, release

    def _issue(
        self,
        *,
        subject_seed: str,
        key: str,
        review_reference: str,
        survey: EncEncuesta | None = None,
        release: SurveyGovernanceRelease | None = None,
    ):
        survey = survey or self.survey
        release = release or self.release
        return issue_eligibility_grant(
            tenant_id=self.tenant.id,
            survey_id=survey.id,
            release_id=release.id,
            actor_user_id=self.owner.id,
            subject_namespace="institution.roster",
            subject_ref=self._opaque_subject(subject_seed),
            authority_namespace="institution.roster",
            authority_adapter_version="adapter-v1",
            review_reference=review_reference,
            expires_at=None,
            idempotency_key=key,
            config=self.gate_config,
            ip_address="127.0.0.1",
        )

    def _response(self, *, survey=None, release=None):
        survey = survey or self.survey
        release = release or self.release
        response = EncRespuesta(
            encuesta_id=survey.id,
            tenant_id=self.tenant.id,
            privacy_mode="legacy",
            governance_release_id=release.id,
            governance_eligibility_policy_version=release.eligibility_policy_version,
            governance_consent_policy_version=release.consent_policy_version,
            governance_acknowledged_at=datetime.now(timezone.utc),
        )
        db.session.add(response)
        db.session.flush()
        return response

    def test_gate_and_public_contract_fail_closed_without_overclaiming(self):
        malformed = dict(self.gate_config)
        malformed["SURVEY_ELIGIBILITY_GRANT_TENANT_IDS"] = "01"
        gate = resolve_eligibility_gate(malformed, tenant_id=self.tenant.id)
        self.assertFalse(gate.ready)
        self.assertEqual(gate.reason_code, "survey_eligibility_allowlist_invalid")

        restricted = public_eligibility_contract(
            self.release, config=self.gate_config
        )
        self.assertTrue(restricted["credential_required"])
        self.assertEqual(
            restricted["transport"]["header_name"],
            "X-Survey-Eligibility-Credential",
        )
        self.assertEqual(
            restricted["assurance_level"], "human_reviewed_opaque_grant"
        )
        self.assertEqual(restricted["authority_binding"], "operator_attested_v1")
        self.assertFalse(restricted["ballot_secrecy_certified"])
        self.assertIsNone(restricted["eligible_population"])

        _, open_release = self._published_release(slug="open-one", mode="open")
        public_open = public_eligibility_contract(open_release, config={})
        self.assertEqual(public_open["privacy_assurance"], "attestation_only")
        self.assertEqual(public_open["assurance_level"], "attestation_only")
        self.assertFalse(public_open["credential_required"])

    def test_issue_replay_privacy_revocation_and_bounded_reissue(self):
        raw_subject = self._opaque_subject("citizen-one")
        raw_review = "case:Review_00000001"
        grant, credential, replayed = self._issue(
            subject_seed="citizen-one",
            key="issue:00000001",
            review_reference=raw_review,
        )
        self.assertFalse(replayed)
        self.assertRegex(credential or "", r"^sec1_[A-Za-z0-9_-]{43}$")
        self.assertEqual(grant.generation, 1)
        self.assertEqual(grant.subject_namespace, "institution.roster")
        issue_receipt = serialize_issue_receipt(
            grant,
            gate=resolve_eligibility_gate(
                self.gate_config, tenant_id=self.tenant.id
            ),
            replayed=False,
        )
        self.assertEqual(issue_receipt["tenant_id"], self.tenant.id)
        self.assertEqual(issue_receipt["survey_id"], self.survey.id)
        self.assertEqual(issue_receipt["release_id"], self.release.id)
        self.assertNotEqual(grant.subject_hmac, raw_subject)
        self.assertNotEqual(grant.review_reference_hmac, raw_review)
        stored = json.dumps(
            {
                column.name: getattr(grant, column.name)
                for column in grant.__table__.columns
                if isinstance(getattr(grant, column.name), (str, int, type(None)))
            },
            sort_keys=True,
        )
        audit_text = json.dumps(
            [event.details for event in AuditEvent.query.all()], sort_keys=True
        )
        self.assertNotIn(raw_subject, stored + audit_text)
        self.assertNotIn(raw_review, stored + audit_text)
        self.assertNotIn(credential or "", stored + audit_text)

        replay_grant, replay_credential, replayed = self._issue(
            subject_seed="citizen-one",
            key="issue:00000001",
            review_reference=raw_review,
        )
        self.assertTrue(replayed)
        self.assertEqual(replay_grant.id, grant.id)
        self.assertEqual(replay_credential, credential)

        with self.assertRaises(SurveyEligibilityError) as conflict:
            self._issue(
                subject_seed="citizen-one",
                key="issue:00000001",
                review_reference="case:Review_00000002",
            )
        self.assertEqual(conflict.exception.reason_code, "survey_eligibility_idempotency_conflict")

        with self.assertRaises(SurveyEligibilityError) as active:
            self._issue(
                subject_seed="citizen-one",
                key="issue:00000002",
                review_reference="case:Review_00000002",
            )
        self.assertEqual(active.exception.reason_code, "survey_eligibility_subject_grant_active")

        terminal, replayed_revoke = None, None
        _, terminal, replayed_revoke = revoke_eligibility_grant(
            tenant_id=self.tenant.id,
            survey_id=self.survey.id,
            release_id=self.release.id,
            grant_ref=grant.grant_ref,
            actor_user_id=self.owner.id,
            reason_code="credential_compromised",
            idempotency_key="revoke:00000001",
            config=self.gate_config,
        )
        self.assertFalse(replayed_revoke)
        self.assertEqual(terminal.disposition, "revoked")
        revocation_receipt = serialize_revocation_receipt(
            grant, terminal, replayed=False
        )
        self.assertEqual(revocation_receipt["tenant_id"], self.tenant.id)
        self.assertEqual(revocation_receipt["survey_id"], self.survey.id)
        self.assertEqual(revocation_receipt["release_id"], self.release.id)
        _, replay_terminal, replayed_revoke = revoke_eligibility_grant(
            tenant_id=self.tenant.id,
            survey_id=self.survey.id,
            release_id=self.release.id,
            grant_ref=grant.grant_ref,
            actor_user_id=self.owner.id,
            reason_code="credential_compromised",
            idempotency_key="revoke:00000001",
            config=self.gate_config,
        )
        self.assertTrue(replayed_revoke)
        self.assertEqual(replay_terminal.id, terminal.id)

        with self.assertRaises(SurveyEligibilityError) as stale_review:
            self._issue(
                subject_seed="citizen-one",
                key="issue:00000005",
                review_reference=raw_review,
            )
        self.assertEqual(
            stale_review.exception.reason_code,
            "survey_eligibility_reissue_review_required",
        )

        second, second_credential, _ = self._issue(
            subject_seed="citizen-one",
            key="issue:00000003",
            review_reference="case:Review_00000003",
        )
        self.assertEqual(second.generation, 2)
        self.assertNotEqual(second_credential, credential)
        revoke_eligibility_grant(
            tenant_id=self.tenant.id,
            survey_id=self.survey.id,
            release_id=self.release.id,
            grant_ref=second.grant_ref,
            actor_user_id=self.owner.id,
            reason_code="subject_ineligible",
            idempotency_key="revoke:00000002",
            config=self.gate_config,
        )
        with self.assertRaises(SurveyEligibilityError) as blocked:
            self._issue(
                subject_seed="citizen-one",
                key="issue:00000004",
                review_reference="case:Review_00000004",
            )
        self.assertEqual(blocked.exception.reason_code, "survey_eligibility_subject_reissue_blocked")

    def test_issue_rejects_release_window_too_short_after_expiry_clamp(self):
        survey, release = self._published_release(
            slug="restricted-closing",
            mode="manual_review",
            ends_in=timedelta(minutes=4),
        )

        with self.assertRaises(SurveyEligibilityError) as closing:
            self._issue(
                subject_seed="citizen-closing",
                key="issue:closing:0001",
                review_reference="case:Closing_00000001",
                survey=survey,
                release=release,
            )

        self.assertEqual(
            closing.exception.reason_code,
            "survey_eligibility_release_window_too_short",
        )
        self.assertEqual(SurveyEligibilityGrant.query.filter_by(survey_id=survey.id).count(), 0)

    def test_atomic_redemption_active_validation_and_public_receipt(self):
        grant, credential, _ = self._issue(
            subject_seed="citizen-redeem",
            key="issue:00000100",
            review_reference="case:Review_00000100",
        )
        locked = lock_eligibility_grant_for_submission(
            encuesta=self.survey,
            release=self.release,
            credential=credential,
            submission_id="submission:00000100",
            commit=True,
            transport="http",
            config=self.gate_config,
        )
        self.assertEqual(locked.id, grant.id)

        response = self._response()
        verified_at = datetime.now(timezone.utc)
        terminal = stage_eligibility_redemption(
            grant=locked,
            respuesta=response,
            submission_payload_hash="d" * 64,
            redeemed_at=verified_at,
        )
        db.session.commit()
        self.assertEqual(response.eligibility_decision, ELIGIBILITY_DECISION_VERIFIED)
        contract = response_eligibility_contract(response)
        self.assertEqual(contract["decision"], ELIGIBILITY_DECISION_VERIFIED)
        self.assertEqual(
            contract["redemption"]["contract_version"],
            SURVEY_ELIGIBILITY_REDEMPTION_CONTRACT_VERSION,
        )
        self.assertEqual(contract["redemption"]["response_id"], response.id)
        self.assertNotIn("grant_ref", json.dumps(contract))
        self.assertNotIn("credential", contract)
        self.assertNotIn("credential", contract["redemption"])
        self.assertNotIn(credential or "", json.dumps(contract))
        self.assertFalse(contract["ballot_secrecy_certified"])

        with self.assertRaises(SurveyEligibilityError) as consumed:
            lock_eligibility_grant_for_submission(
                encuesta=self.survey,
                release=self.release,
                credential=credential,
                submission_id="submission:00000101",
                commit=True,
                transport="http",
                config=self.gate_config,
            )
        self.assertEqual(consumed.exception.status_code, 409)
        self.assertEqual(consumed.exception.reason_code, "survey_eligibility_credential_consumed")

        terminal.request_hash = "e" * 64
        with self.assertRaises(ValueError):
            db.session.flush()
        db.session.rollback()

    def test_submission_transport_scope_expiry_and_corrupt_receipt_fail_closed(self):
        grant, credential, _ = self._issue(
            subject_seed="citizen-validation",
            key="issue:00000200",
            review_reference="case:Review_00000200",
        )
        with self.assertRaises(SurveyEligibilityError) as transport:
            lock_eligibility_grant_for_submission(
                encuesta=self.survey,
                release=self.release,
                credential=credential,
                submission_id="submission:00000200",
                commit=False,
                transport="meta_flow",
                config=self.gate_config,
            )
        self.assertEqual(transport.exception.reason_code, "survey_eligibility_transport_unsupported")
        with self.assertRaises(SurveyEligibilityError) as missing:
            lock_eligibility_grant_for_submission(
                encuesta=self.survey,
                release=self.release,
                credential=None,
                submission_id="submission:00000201",
                commit=True,
                transport="http",
                config=self.gate_config,
            )
        self.assertEqual(missing.exception.status_code, 428)

        other_survey, other_release = self._published_release(
            slug="restricted-two", mode="manual_review"
        )
        with self.assertRaises(SurveyEligibilityError) as wrong_scope:
            lock_eligibility_grant_for_submission(
                encuesta=other_survey,
                release=other_release,
                credential=credential,
                submission_id="submission:00000202",
                commit=True,
                transport="http",
                config=self.gate_config,
            )
        self.assertEqual(wrong_scope.exception.status_code, 403)
        with self.assertRaises(SurveyEligibilityError) as expired:
            assert_eligibility_grant_active(
                grant, now=grant.expires_at + timedelta(seconds=1)
            )
        self.assertEqual(expired.exception.status_code, 410)

        orphan = self._response()
        with self.assertRaises(SurveyEligibilityError) as unverified:
            response_eligibility_contract(orphan)
        self.assertEqual(
            unverified.exception.reason_code,
            "survey_eligibility_response_receipt_corrupt",
        )
        orphan.eligibility_contract_version = "surveys.public_eligibility.v1"
        orphan.eligibility_decision = ELIGIBILITY_DECISION_VERIFIED
        orphan.eligibility_verified_at = datetime.now(timezone.utc)
        db.session.commit()
        with self.assertRaises(SurveyEligibilityError) as corrupt:
            response_eligibility_contract(orphan)
        self.assertEqual(
            corrupt.exception.reason_code,
            "survey_eligibility_response_receipt_corrupt",
        )

        aggregate = eligibility_aggregate(
            tenant_id=self.tenant.id,
            survey_id=self.survey.id,
            release_id=self.release.id,
            config=self.gate_config,
        )
        self.assertEqual(aggregate["counts"]["issued"], 1)
        self.assertIsNone(aggregate["eligible_population"])
        self.assertNotIn("grants", aggregate)
        self.assertNotIn("subjects", aggregate)

    def test_opaque_subject_and_authority_binding_are_mandatory(self):
        with self.assertRaises(SurveyEligibilityError) as raw_subject:
            issue_eligibility_grant(
                tenant_id=self.tenant.id,
                survey_id=self.survey.id,
                release_id=self.release.id,
                actor_user_id=self.owner.id,
                subject_namespace="institution.roster",
                subject_ref="32877851",
                authority_namespace="institution.roster",
                authority_adapter_version="adapter-v1",
                review_reference="case:Review_00000300",
                expires_at=None,
                idempotency_key="issue:00000300",
                config=self.gate_config,
            )
        self.assertEqual(
            raw_subject.exception.reason_code,
            "survey_eligibility_subject_opaque_ref_required",
        )
        with self.assertRaises(SurveyEligibilityError) as alias:
            issue_eligibility_grant(
                tenant_id=self.tenant.id,
                survey_id=self.survey.id,
                release_id=self.release.id,
                actor_user_id=self.owner.id,
                subject_namespace="document.alias",
                subject_ref=self._opaque_subject("citizen-alias"),
                authority_namespace="institution.roster",
                authority_adapter_version="adapter-v1",
                review_reference="case:Review_00000301",
                expires_at=None,
                idempotency_key="issue:00000301",
                config=self.gate_config,
            )
        self.assertEqual(
            alias.exception.reason_code,
            "survey_eligibility_authority_binding_invalid",
        )


if __name__ == "__main__":
    unittest.main()
