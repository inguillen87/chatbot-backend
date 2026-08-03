import base64
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jwt
import webauthn
from flask import g
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app import create_app
from config import TestConfig
from extensions import db
from models import TenantProfile, User, WebAuthnCredential, generate_token


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _attestation(credential_id: str) -> dict:
    return {
        "id": credential_id,
        "rawId": credential_id,
        "type": "public-key",
        "response": {
            "clientDataJSON": "e30",
            "attestationObject": "e30",
            "transports": ["internal"],
        },
    }


def _assertion(credential_id: str) -> dict:
    return {
        "id": credential_id,
        "rawId": credential_id,
        "type": "public-key",
        "response": {
            "clientDataJSON": "e30",
            "authenticatorData": "e30",
            "signature": "e30",
        },
    }


class WebAuthnSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app(TestConfig)
        cls.app_context = cls.app.app_context()
        cls.app_context.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.app_context.pop()

    def setUp(self):
        # The suite keeps one outer application context for the in-memory DB;
        # clear Flask-Login's context-local cache so identities never leak
        # between otherwise independent test clients.
        g.pop("_login_user", None)
        g.viewer = None
        db.session.query(WebAuthnCredential).delete()
        db.session.query(TenantProfile).delete()
        db.session.query(User).delete()
        db.session.commit()
        self.client = self.app.test_client()

    def _new_user(self, email: str = "owner@example.com") -> User:
        user = User(
            name="Owner",
            email=email,
            token=generate_token(),
            rol="usuario",
        )
        user.set_password("test-only-password")
        db.session.add(user)
        db.session.commit()
        return user

    def _bearer(self, user: User) -> dict:
        token = jwt.encode(
            {
                "user_id": user.id,
                "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
            },
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        return {"Authorization": f"Bearer {token}"}

    def _credential_for(
        self,
        user: User,
        *,
        credential_bytes: bytes = b"login-credential",
        public_key: bytes = b"login-public-key",
        sign_count: int = 0,
    ) -> WebAuthnCredential:
        credential = WebAuthnCredential(
            user_id=user.id,
            credential_id=_b64url(credential_bytes),
            public_key=_b64url(public_key),
            sign_count=sign_count,
        )
        db.session.add(credential)
        db.session.commit()
        return credential

    def _passkey_login(
        self,
        credential: WebAuthnCredential,
        *,
        client=None,
        new_sign_count: int | None = None,
    ):
        login_client = client or self.client
        self.assertEqual(
            login_client.get("/api/webauthn/login/options").status_code,
            200,
        )
        if new_sign_count is None:
            new_sign_count = credential.sign_count
        with patch(
            "routes.webauthn.verify_authentication_response",
            return_value=SimpleNamespace(new_sign_count=new_sign_count),
        ), patch("routes.webauthn.merge_anon_into_user"):
            return login_client.post(
                "/api/webauthn/login/verify",
                json={
                    "authenticationResponse": _assertion(
                        credential.credential_id
                    )
                },
            )

    @staticmethod
    def _registration_verification(
        credential_bytes: bytes = b"new-credential",
    ) -> SimpleNamespace:
        return SimpleNamespace(
            credential_id=credential_bytes,
            credential_public_key=b"new-public-key",
            sign_count=0,
        )

    def test_real_webauthn_library_generates_browser_compatible_options(self):
        self.assertIsNotNone(getattr(webauthn, "__file__", None))
        module_path = Path(str(webauthn.__file__))
        self.assertEqual(module_path.name, "__init__.py")
        self.assertEqual(module_path.parent.name, "webauthn")

        response = self.client.get("/api/webauthn/register/options")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["rp"]["id"], "chatboc.ar")
        self.assertEqual(len(base64.urlsafe_b64decode(payload["user"]["id"] + "==")), 32)
        self.assertGreaterEqual(len(base64.urlsafe_b64decode(payload["challenge"] + "==")), 16)
        self.assertEqual(payload["attestation"], "none")
        self.assertEqual(User.query.count(), 0)

    def test_origin_guards_reject_cross_site_challenge_rotation(self):
        with patch("routes.webauthn.generate_authentication_options") as generate:
            hostile_origin = self.client.get(
                "/api/webauthn/login/options",
                headers={"Origin": "https://attacker.example"},
            )
            no_origin_cross_site = self.client.get(
                "/api/webauthn/login/options",
                headers={"Sec-Fetch-Site": "cross-site"},
            )

        self.assertEqual(hostile_origin.status_code, 403)
        self.assertEqual(no_origin_cross_site.status_code, 403)
        generate.assert_not_called()

        allowed = self.client.get(
            "/api/webauthn/login/options",
            headers={"Origin": "https://www.chatboc.ar"},
        )
        self.assertEqual(allowed.status_code, 200)

    def test_invalid_rp_configuration_fails_closed_without_using_host_header(self):
        previous = self.app.config.get("WEBAUTHN_RP_ID")
        self.app.config["WEBAUTHN_RP_ID"] = ""
        try:
            with patch("routes.webauthn.generate_authentication_options") as generate:
                response = self.client.get(
                    "/api/webauthn/login/options",
                    headers={"Host": "attacker.example"},
                )
        finally:
            self.app.config["WEBAUTHN_RP_ID"] = previous

        self.assertEqual(response.status_code, 503)
        generate.assert_not_called()

    def test_untrusted_anon_cannot_select_or_precreate_passkey_user(self):
        attacker_chosen_anon = "attacker-fixed-identity"
        options_response = self.client.get(
            "/api/webauthn/register/options?display_name=Vecina",
            headers={"X-Anon-Id": attacker_chosen_anon},
        )

        self.assertEqual(options_response.status_code, 200)
        self.assertEqual(User.query.count(), 0)
        issued_anon = options_response.headers["X-Anon-Id"]
        self.assertNotEqual(issued_anon, attacker_chosen_anon)
        self.assertEqual(len(issued_anon), 32)

        credential_id = _b64url(b"fresh-credential")
        verification = SimpleNamespace(
            credential_id=b"fresh-credential",
            credential_public_key=b"fresh-public-key",
            sign_count=0,
        )
        with patch(
            "routes.webauthn.verify_registration_response",
            return_value=verification,
        ) as verify:
            verify_response = self.client.post(
                "/api/webauthn/register/verify",
                json={"attestationResponse": _attestation(credential_id)},
                headers={"X-Anon-Id": attacker_chosen_anon},
            )

        self.assertEqual(verify_response.status_code, 200)
        self.assertTrue(verify_response.get_json()["ok"])
        user = User.query.one()
        credential = WebAuthnCredential.query.one()
        self.assertEqual(user.anon_id, issued_anon)
        self.assertNotEqual(user.anon_id, attacker_chosen_anon)
        self.assertEqual(credential.user_id, user.id)
        self.assertEqual(credential.transports, ["internal"])
        verify_kwargs = verify.call_args.kwargs
        self.assertIsInstance(verify_kwargs["credential"], dict)
        self.assertIsInstance(verify_kwargs["expected_challenge"], bytes)

    def test_failed_attestation_does_not_create_provisional_identity(self):
        self.assertEqual(
            self.client.get("/api/webauthn/register/options").status_code,
            200,
        )

        with patch(
            "routes.webauthn.verify_registration_response",
            side_effect=ValueError("invalid attestation"),
        ):
            response = self.client.post(
                "/api/webauthn/register/verify",
                json={"attestationResponse": _attestation(_b64url(b"invalid"))},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(User.query.count(), 0)
        self.assertEqual(WebAuthnCredential.query.count(), 0)

    def test_real_verifier_rejects_malformed_attestation_without_identity(self):
        self.assertEqual(
            self.client.get("/api/webauthn/register/options").status_code,
            200,
        )

        response = self.client.post(
            "/api/webauthn/register/verify",
            json={"attestationResponse": _attestation(_b64url(b"invalid-real"))},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(User.query.count(), 0)
        self.assertEqual(WebAuthnCredential.query.count(), 0)

    def test_registration_challenge_expires_and_is_consumed(self):
        with patch("routes.webauthn.time.time", return_value=1_000.0):
            self.assertEqual(
                self.client.get("/api/webauthn/register/options").status_code,
                200,
            )

        with patch("routes.webauthn.time.time", return_value=1_301.0), patch(
            "routes.webauthn.verify_registration_response"
        ) as verify:
            expired = self.client.post(
                "/api/webauthn/register/verify",
                json={"attestationResponse": _attestation(_b64url(b"expired"))},
            )
            replay = self.client.post(
                "/api/webauthn/register/verify",
                json={"attestationResponse": _attestation(_b64url(b"expired"))},
            )

        self.assertEqual(expired.status_code, 400)
        self.assertEqual(replay.status_code, 400)
        verify.assert_not_called()
        self.assertEqual(User.query.count(), 0)

    def test_registration_integrity_failure_rolls_back_user_and_credential(self):
        self.assertEqual(
            self.client.get("/api/webauthn/register/options").status_code,
            200,
        )
        integrity_error = IntegrityError(
            "insert failed",
            params={},
            orig=RuntimeError("duplicate"),
        )
        with patch(
            "routes.webauthn.verify_registration_response",
            return_value=self._registration_verification(b"rollback-credential"),
        ), patch(
            "routes.webauthn.db.session.commit",
            side_effect=integrity_error,
        ):
            response = self.client.post(
                "/api/webauthn/register/verify",
                json={
                    "attestationResponse": _attestation(
                        _b64url(b"rollback-credential")
                    )
                },
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(User.query.count(), 0)
        self.assertEqual(WebAuthnCredential.query.count(), 0)

    def test_authenticated_enrollment_is_bound_to_same_authenticated_user(self):
        user = self._new_user()
        headers = self._bearer(user)

        self.assertEqual(
            self.client.get(
                "/api/webauthn/register/options",
                headers=headers,
            ).status_code,
            200,
        )
        rejected = self.client.post(
            "/api/webauthn/register/verify",
            json={"attestationResponse": _attestation(_b64url(b"credential-one"))},
        )
        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(WebAuthnCredential.query.count(), 0)

        self.assertEqual(
            self.client.get(
                "/api/webauthn/register/options",
                headers=headers,
            ).status_code,
            200,
        )
        verification = SimpleNamespace(
            credential_id=b"credential-two",
            credential_public_key=b"public-key-two",
            sign_count=0,
        )
        with patch(
            "routes.webauthn.verify_registration_response",
            return_value=verification,
        ):
            enrolled = self.client.post(
                "/api/webauthn/register/verify",
                json={
                    "attestationResponse": _attestation(
                        _b64url(b"credential-two")
                    )
                },
                headers=headers,
            )

        self.assertEqual(enrolled.status_code, 200)
        self.assertEqual(User.query.count(), 1)
        self.assertEqual(WebAuthnCredential.query.one().user_id, user.id)
        self.assertEqual(enrolled.get_json()["account_state"], "authenticated")

    def test_provisional_ceremony_rejects_authenticated_identity_transition(self):
        self.assertEqual(
            self.client.get("/api/webauthn/register/options").status_code,
            200,
        )
        authenticated_user = self._new_user("signed-in@example.com")

        with patch(
            "routes.webauthn.verify_registration_response",
            return_value=self._registration_verification(b"transition-credential"),
        ) as verify:
            response = self.client.post(
                "/api/webauthn/register/verify",
                json={
                    "attestationResponse": _attestation(
                        _b64url(b"transition-credential")
                    )
                },
                headers=self._bearer(authenticated_user),
            )

        self.assertEqual(response.status_code, 401)
        verify.assert_not_called()
        self.assertEqual(User.query.count(), 1)
        self.assertEqual(WebAuthnCredential.query.count(), 0)

    def test_explicit_bearer_overrides_ambient_logged_in_identity(self):
        ambient_user = self._new_user("ambient@example.com")
        ambient_credential = self._credential_for(
            ambient_user,
            credential_bytes=b"ambient-login",
        )
        self.assertEqual(self._passkey_login(ambient_credential).status_code, 200)

        bearer_user = self._new_user("bearer@example.com")
        bearer_headers = self._bearer(bearer_user)
        self.assertEqual(
            self.client.get(
                "/api/webauthn/register/options",
                headers=bearer_headers,
            ).status_code,
            200,
        )

        with patch(
            "routes.webauthn.verify_registration_response",
            return_value=self._registration_verification(b"bearer-enrollment"),
        ), patch("routes.webauthn.merge_anon_into_user"):
            enrolled = self.client.post(
                "/api/webauthn/register/verify",
                json={
                    "attestationResponse": _attestation(
                        _b64url(b"bearer-enrollment")
                    )
                },
                headers=bearer_headers,
            )

        self.assertEqual(enrolled.status_code, 200)
        stored = WebAuthnCredential.query.filter_by(
            credential_id=_b64url(b"bearer-enrollment")
        ).one()
        self.assertEqual(stored.user_id, bearer_user.id)

    def test_invalid_explicit_bearer_never_falls_back_to_ambient_session(self):
        ambient_user = self._new_user("ambient-invalid@example.com")
        ambient_credential = self._credential_for(
            ambient_user,
            credential_bytes=b"ambient-invalid-login",
        )
        self.assertEqual(self._passkey_login(ambient_credential).status_code, 200)

        response = self.client.get(
            "/api/webauthn/register/options",
            headers={"Authorization": "Bearer invalid-opaque-token"},
        )

        self.assertEqual(response.status_code, 401)

    def test_registration_and_login_challenges_are_not_interchangeable(self):
        login_only_client = self.app.test_client()
        self.assertEqual(
            login_only_client.get("/api/webauthn/login/options").status_code,
            200,
        )
        wrong_registration = login_only_client.post(
            "/api/webauthn/register/verify",
            json={"attestationResponse": _attestation(_b64url(b"credential"))},
        )
        self.assertEqual(wrong_registration.status_code, 400)

        registration_only_client = self.app.test_client()
        self.assertEqual(
            registration_only_client.get(
                "/api/webauthn/register/options"
            ).status_code,
            200,
        )
        wrong_login = registration_only_client.post(
            "/api/webauthn/login/verify",
            json={"authenticationResponse": _assertion(_b64url(b"credential"))},
        )
        self.assertEqual(wrong_login.status_code, 400)
        self.assertEqual(User.query.count(), 0)

    def test_login_response_contract_is_nested_tenant_safe_and_not_replayable(self):
        user = self._new_user("tenant-owner@example.com")
        tenant = TenantProfile(
            slug="junin-seguro",
            nombre="Municipalidad de Junín",
            tipo="municipio",
            municipio_id=user.id,
        )
        db.session.add(tenant)
        db.session.commit()
        credential = self._credential_for(
            user,
            credential_bytes=b"tenant-login",
        )

        response = self._passkey_login(credential)

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["id"], user.id)
        self.assertEqual(payload["tenant_slug"], tenant.slug)
        self.assertEqual(payload["tenantSlug"], tenant.slug)
        self.assertEqual(payload["tipo_chat"], "municipio")
        self.assertEqual(payload["user"]["id"], user.id)
        self.assertEqual(payload["user"]["rol"], user.rol)
        self.assertEqual(payload["user"]["role"], user.rol)
        self.assertEqual(payload["user"]["tenant_slug"], tenant.slug)
        self.assertNotIn("token", payload["user"])
        self.assertNotIn("entity_token", payload)
        self.assertNotIn("entityToken", payload)
        self.assertNotIn("entity_token", payload["user"])

        with patch("routes.webauthn.verify_authentication_response") as verify:
            replay = self.client.post(
                "/api/webauthn/login/verify",
                json={
                    "authenticationResponse": _assertion(
                        credential.credential_id
                    )
                },
            )
        self.assertEqual(replay.status_code, 400)
        verify.assert_not_called()

    def test_successful_passkey_login_rotates_pre_authentication_session_id(self):
        user = self._new_user("sid-rotation@example.com")
        credential = self._credential_for(
            user,
            credential_bytes=b"sid-rotation-login",
        )
        self.assertEqual(
            self.client.get("/api/webauthn/login/options").status_code,
            200,
        )
        cookie_name = self.app.config.get("SESSION_COOKIE_NAME", "session")
        old_cookie = self.client.get_cookie(cookie_name)
        self.assertIsNotNone(old_cookie)

        with patch(
            "routes.webauthn.verify_authentication_response",
            return_value=SimpleNamespace(new_sign_count=0),
        ), patch("routes.webauthn.merge_anon_into_user"):
            response = self.client.post(
                "/api/webauthn/login/verify",
                json={
                    "authenticationResponse": _assertion(
                        credential.credential_id
                    )
                },
            )

        self.assertEqual(response.status_code, 200)
        new_cookie = self.client.get_cookie(cookie_name)
        self.assertIsNotNone(new_cookie)
        self.assertNotEqual(old_cookie.value, new_cookie.value)

        fixed_client = self.app.test_client()
        fixed_client.set_cookie(cookie_name, old_cookie.value)
        with fixed_client.session_transaction() as fixed_session:
            self.assertNotIn("_user_id", fixed_session)

    def test_login_challenge_is_bound_to_session_and_expires(self):
        user = self._new_user("expiry@example.com")
        credential = self._credential_for(
            user,
            credential_bytes=b"expiry-login",
        )
        foreign_client = self.app.test_client()

        with patch("routes.webauthn.time.time", return_value=2_000.0):
            self.assertEqual(
                self.client.get("/api/webauthn/login/options").status_code,
                200,
            )

        with patch(
            "routes.webauthn.verify_authentication_response",
            return_value=SimpleNamespace(new_sign_count=0),
        ) as verify:
            foreign_response = foreign_client.post(
                "/api/webauthn/login/verify",
                json={
                    "authenticationResponse": _assertion(
                        credential.credential_id
                    )
                },
            )
        self.assertEqual(foreign_response.status_code, 400)
        verify.assert_not_called()

        with patch("routes.webauthn.time.time", return_value=2_301.0), patch(
            "routes.webauthn.verify_authentication_response"
        ) as verify:
            expired = self.client.post(
                "/api/webauthn/login/verify",
                json={
                    "authenticationResponse": _assertion(
                        credential.credential_id
                    )
                },
            )
        self.assertEqual(expired.status_code, 400)
        verify.assert_not_called()

    def test_unknown_credential_and_real_invalid_assertion_are_generic(self):
        unknown_client = self.app.test_client()
        self.assertEqual(
            unknown_client.get("/api/webauthn/login/options").status_code,
            200,
        )
        unknown = unknown_client.post(
            "/api/webauthn/login/verify",
            json={
                "authenticationResponse": _assertion(
                    _b64url(b"unknown-credential")
                )
            },
        )
        self.assertEqual(unknown.status_code, 401)
        self.assertNotIn("no registrada", unknown.get_data(as_text=True).lower())

        user = self._new_user("invalid-assertion@example.com")
        credential = self._credential_for(
            user,
            credential_bytes=b"invalid-assertion",
        )
        self.assertEqual(
            self.client.get("/api/webauthn/login/options").status_code,
            200,
        )
        invalid = self.client.post(
            "/api/webauthn/login/verify",
            json={
                "authenticationResponse": _assertion(
                    credential.credential_id
                )
            },
        )
        self.assertEqual(invalid.status_code, 401)
        db.session.expire_all()
        self.assertEqual(
            db.session.get(WebAuthnCredential, credential.id).sign_count,
            0,
        )

    def test_disabled_account_cannot_authenticate_with_existing_passkey(self):
        user = self._new_user("disabled@example.com")
        user.accesibilidad = {"auth": {"disabled": True}}
        db.session.add(user)
        db.session.commit()
        credential = self._credential_for(
            user,
            credential_bytes=b"disabled-login",
        )

        self.assertEqual(
            self.client.get("/api/webauthn/login/options").status_code,
            200,
        )
        with patch(
            "routes.webauthn.verify_authentication_response",
            return_value=SimpleNamespace(new_sign_count=1),
        ):
            response = self.client.post(
                "/api/webauthn/login/verify",
                json={
                    "authenticationResponse": _assertion(
                        credential.credential_id
                    )
                },
            )

        self.assertEqual(response.status_code, 401)
        self.assertNotIn("auth_token=", response.headers.get("Set-Cookie", ""))
        db.session.expire_all()
        self.assertEqual(
            db.session.get(WebAuthnCredential, credential.id).sign_count,
            0,
        )

    def test_login_counter_update_is_compare_and_swap(self):
        user = self._new_user()
        credential = self._credential_for(user)
        credential_id = credential.credential_id

        self.assertEqual(
            self.client.get("/api/webauthn/login/options").status_code,
            200,
        )

        query_type = type(WebAuthnCredential.query)
        with patch(
            "routes.webauthn.verify_authentication_response",
            return_value=SimpleNamespace(new_sign_count=1),
        ) as verify, patch.object(query_type, "update", return_value=0):
            response = self.client.post(
                "/api/webauthn/login/verify",
                json={"authenticationResponse": _assertion(credential_id)},
            )

        self.assertEqual(response.status_code, 409)
        db.session.expire_all()
        self.assertEqual(db.session.get(WebAuthnCredential, credential.id).sign_count, 0)
        verify_kwargs = verify.call_args.kwargs
        self.assertIsInstance(verify_kwargs["credential"], dict)
        self.assertIsInstance(verify_kwargs["expected_challenge"], bytes)

    def test_login_counter_commit_failure_rolls_back_and_emits_no_session(self):
        user = self._new_user("counter-failure@example.com")
        credential = self._credential_for(
            user,
            credential_bytes=b"counter-failure",
        )
        self.assertEqual(
            self.client.get("/api/webauthn/login/options").status_code,
            200,
        )

        with patch(
            "routes.webauthn.verify_authentication_response",
            return_value=SimpleNamespace(new_sign_count=1),
        ), patch(
            "routes.webauthn.db.session.commit",
            side_effect=SQLAlchemyError("commit unavailable"),
        ):
            response = self.client.post(
                "/api/webauthn/login/verify",
                json={
                    "authenticationResponse": _assertion(
                        credential.credential_id
                    )
                },
            )

        self.assertEqual(response.status_code, 503)
        self.assertNotIn("auth_token=", response.headers.get("Set-Cookie", ""))
        db.session.expire_all()
        self.assertEqual(
            db.session.get(WebAuthnCredential, credential.id).sign_count,
            0,
        )


if __name__ == "__main__":
    unittest.main()
