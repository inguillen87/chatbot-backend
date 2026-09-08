import json
import math
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from sqlalchemy.exc import IntegrityError

from app import create_app, db
from config import Config
from models import MunicipioTicket, TenantProfile, TenantTicket, User
from services.tenant_claim_receipts import (
    TENANT_CLAIM_RECEIPT_SECRET_VERSION,
    normalize_tenant_claim_payload,
    tenant_claim_idempotency_hash,
    tenant_claim_receipt_secret,
)
from utils.auth_helpers import generar_token


TEST_RECEIPT_SECRET = "tenant-claim-receipt-test-secret-v1-0123456789"


class TenantClaimReceiptTestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False
    RATELIMIT_ENABLED = False
    TENANT_CLAIM_RECEIPT_SECRET_V1 = TEST_RECEIPT_SECRET


class TenantClaimReceiptTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TenantClaimReceiptTestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

        self.owner = User(
            name="Tenant Owner",
            email="receipt-owner@test.com",
            password_hash="test-hash",
            rol="admin",
        )
        self.second_owner = User(
            name="Other Owner",
            email="receipt-other@test.com",
            password_hash="test-hash",
            rol="admin",
        )
        db.session.add_all([self.owner, self.second_owner])
        db.session.flush()
        self.tenant = TenantProfile(
            slug="receipt-tenant",
            nombre="Receipt Tenant",
            tipo="municipio",
            municipio_id=self.owner.id,
        )
        self.other_tenant = TenantProfile(
            slug="receipt-other",
            nombre="Other Tenant",
            tipo="municipio",
            municipio_id=self.second_owner.id,
        )
        db.session.add_all([self.tenant, self.other_tenant])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _headers(self, *, tenant=None, key="claim-intake-key-0001", **extra):
        headers = {"X-Tenant-Slug": (tenant or self.tenant).slug}
        if key is not None:
            headers["Idempotency-Key"] = key
        headers.update(extra)
        return headers

    def _post(self, payload=None, *, tenant=None, key="claim-intake-key-0001", legacy=False, **headers):
        return self.client.post(
            "/app/tickets" if legacy else "/api/pwa/app/tickets",
            json=payload if payload is not None else {"descripcion": "Alumbrado apagado"},
            headers=self._headers(tenant=tenant, key=key, **headers),
        )

    def _assert_receipt_contract(self, payload, *, deduplicated):
        self.assertEqual(
            set(payload),
            {
                "contract_version",
                "ok",
                "persisted",
                "deduplicated",
                "request_id",
                "claim",
                "access",
                "tracking",
                "actions",
            },
        )
        self.assertEqual(payload["contract_version"], "claims.intake_receipt.v1")
        self.assertIs(payload["ok"], True)
        self.assertIs(payload["persisted"], True)
        self.assertIs(payload["deduplicated"], deduplicated)
        self.assertTrue(payload["request_id"])
        ticket_id = payload["claim"]["id"]
        self.assertIsInstance(ticket_id, int)
        self.assertGreater(ticket_id, 0)
        code = f"T-{ticket_id}"
        self.assertEqual(payload["claim"]["code"], code)
        self.assertTrue(payload["claim"]["status"])
        self.assertTrue(payload["claim"]["created_at"].endswith("Z"))
        pin = payload["access"]["pin"]
        self.assertEqual(payload["access"]["mode"], "code_pin")
        self.assertRegex(pin, r"^\d{6}$")
        expected_path = f"/tracking/claim/{code}#pin={pin}"
        self.assertEqual(
            payload["tracking"],
            {
                "path": expected_path,
                "experience_endpoint": f"/api/public/tracking/experience?kind=claim&code={code}",
                "credential_transport": "x-tracking-pin-header",
                "requires_pin": True,
            },
        )
        self.assertIn(
            {"id": "track_claim", "label": "Ver seguimiento", "href": expected_path},
            payload["actions"],
        )

    def test_create_returns_exact_receipt_and_persists_only_hashes(self):
        raw_key = "claim-intake-secure-key-0001"
        response = self._post(
            {
                "descripcion": "  Semaforo roto\r\nfrente a la plaza  ",
                "categoria": "  transito  ",
                "lat": "-34.6037",
                "lng": -58.3816,
                "metadata": {"source": "pwa", "nested": {"priority": 2}},
            },
            key=raw_key,
            **{"X-Request-Id": "receipt-create-1"},
        )

        self.assertEqual(response.status_code, 201)
        payload = response.get_json()
        self._assert_receipt_contract(payload, deduplicated=False)
        self.assertEqual(payload["request_id"], "receipt-create-1")
        self.assertEqual(response.headers["X-Request-Id"], "receipt-create-1")
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        self.assertEqual(response.headers["Pragma"], "no-cache")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertNotIn("ticket_id", payload)
        self.assertNotIn("estado", payload)

        ticket = db.session.get(TenantTicket, payload["claim"]["id"])
        self.assertEqual(ticket.descripcion, "Semaforo roto\nfrente a la plaza")
        self.assertEqual(ticket.categoria, "transito")
        self.assertAlmostEqual(ticket.latitud, -34.6037)
        self.assertAlmostEqual(ticket.longitud, -58.3816)
        self.assertEqual(ticket.datos_extra, {"source": "pwa", "nested": {"priority": 2}})
        self.assertRegex(ticket.intake_idempotency_hash, r"^[0-9a-f]{64}$")
        self.assertRegex(ticket.intake_payload_hash, r"^[0-9a-f]{64}$")
        self.assertNotEqual(ticket.intake_idempotency_hash, raw_key)
        self.assertEqual(ticket.claim_receipt_secret_version, "v1")

        persisted_snapshot = json.dumps(
            {
                column.name: getattr(ticket, column.name)
                for column in TenantTicket.__table__.columns
                if column.name not in {"created_at", "updated_at"}
            },
            default=str,
            sort_keys=True,
        )
        self.assertNotIn(payload["access"]["pin"], persisted_snapshot)
        self.assertNotIn(raw_key, persisted_snapshot)

    def test_replay_is_200_same_ticket_and_pin_while_changed_payload_conflicts(self):
        key = "claim-intake-replay-key-0001"
        first = self._post(
            {
                "descripcion": "  Bache profundo ",
                "categoria": " calles ",
                "lat": "-34.0",
                "lng": "-58.0",
                "metadata": {"b": 2, "a": 1},
            },
            key=key,
        )
        replay = self._post(
            {
                "descripcion": "Bache profundo",
                "categoria": "calles",
                "lat": -34,
                "lng": -58.0,
                "extras": {"a": 1, "b": 2},
            },
            key=key,
        )
        conflict = self._post(
            {"descripcion": "Otro reclamo", "categoria": "calles"},
            key=key,
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(replay.status_code, 200)
        first_payload = first.get_json()
        replay_payload = replay.get_json()
        self._assert_receipt_contract(replay_payload, deduplicated=True)
        self.assertEqual(replay_payload["claim"], first_payload["claim"])
        self.assertEqual(replay_payload["access"], first_payload["access"])
        self.assertEqual(replay_payload["tracking"], first_payload["tracking"])
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.get_json()["reason_code"], "idempotency_key_conflict")
        self.assertEqual(TenantTicket.query.count(), 1)

    def test_same_key_is_isolated_by_tenant(self):
        key = "claim-intake-shared-key-0001"
        first = self._post(key=key)
        second = self._post(key=key, tenant=self.other_tenant)

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertNotEqual(first.get_json()["claim"]["id"], second.get_json()["claim"]["id"])
        rows = TenantTicket.query.order_by(TenantTicket.id).all()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0].intake_idempotency_hash, rows[1].intake_idempotency_hash)
        self.assertEqual({row.tenant_id for row in rows}, {self.tenant.id, self.other_tenant.id})

    def test_receipt_ticket_list_cannot_be_recovered_by_spoofing_fingerprint(self):
        creator = self.app.test_client()
        attacker = self.app.test_client()
        fingerprint_headers = {
            "X-Tenant-Slug": self.tenant.slug,
            "X-Forwarded-For": "203.0.113.44",
            "User-Agent": "spoofable-browser/1.0",
            "Accept-Language": "es-AR",
        }
        created = creator.post(
            "/api/pwa/app/tickets",
            json={
                "descripcion": "DESCRIPCION PRIVADA DEL RECIBO",
                "metadata": {"private_note": "EXTRA PRIVADO DEL RECIBO"},
            },
            headers={
                **fingerprint_headers,
                "Idempotency-Key": "claim-intake-fingerprint-0001",
            },
        )
        self.assertEqual(created.status_code, 201)
        receipt_ticket = db.session.get(TenantTicket, created.get_json()["claim"]["id"])
        self.assertIsNotNone(receipt_ticket.claim_receipt_secret_version)
        self.assertTrue(receipt_ticket.fingerprint)

        # This represents a row created before receipt credentials existed.
        legacy = TenantTicket(
            tenant_id=self.tenant.id,
            descripcion="Legacy visible por fingerprint",
            datos_extra={"legacy": True},
            fingerprint=receipt_ticket.fingerprint,
        )
        db.session.add(legacy)
        db.session.commit()

        # Even a real tenant admin is not the authenticated owner of the
        # anonymous receipt row, so role plus spoofed headers cannot expose it.
        admin_token = generar_token(self.owner.id, self.owner.rol, None, None, None)
        attacker.set_cookie("auth_token", admin_token)
        response = attacker.get("/api/pwa/app/tickets", headers=fingerprint_headers)

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual([item["id"] for item in payload["tickets"]], [legacy.id])
        self.assertEqual(payload["summary"]["total"], 1)
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        self.assertEqual(response.headers["Pragma"], "no-cache")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        serialized = response.get_data(as_text=True)
        self.assertNotIn("DESCRIPCION PRIVADA DEL RECIBO", serialized)
        self.assertNotIn("EXTRA PRIVADO DEL RECIBO", serialized)

    def test_authenticated_ticket_owner_can_list_receipt_without_fingerprint_match(self):
        claimant = User(
            name="Claimant",
            email="claimant-receipt@test.com",
            password_hash="test-hash",
            rol="usuario",
        )
        db.session.add(claimant)
        db.session.commit()
        token = generar_token(claimant.id, claimant.rol, None, None, None)
        creator = self.app.test_client()
        owner_client = self.app.test_client()
        creator.set_cookie("auth_token", token)
        owner_client.set_cookie("auth_token", token)

        created = creator.post(
            "/api/pwa/app/tickets",
            json={"descripcion": "Reclamo propio autenticado"},
            headers={
                "X-Tenant-Slug": self.tenant.slug,
                "Idempotency-Key": "claim-intake-owner-list-0001",
                "User-Agent": "creator-browser",
            },
        )
        self.assertEqual(created.status_code, 201)
        response = owner_client.get(
            "/api/pwa/app/tickets",
            headers={
                "X-Tenant-Slug": self.tenant.slug,
                "X-Forwarded-For": "198.51.100.99",
                "User-Agent": "different-owner-browser",
                "Accept-Language": "en-US",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(len(payload["tickets"]), 1)
        self.assertEqual(payload["tickets"][0]["id"], created.get_json()["claim"]["id"])
        self.assertEqual(payload["tickets"][0]["descripcion"], "Reclamo propio autenticado")
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        self.assertEqual(response.headers["Pragma"], "no-cache")

    def test_canonical_requires_key_but_legacy_alias_without_key_returns_receipt(self):
        missing = self._post(key=None)
        legacy = self._post(key=None, legacy=True)

        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.get_json()["reason_code"], "idempotency_key_required")
        self.assertEqual(legacy.status_code, 201)
        self._assert_receipt_contract(legacy.get_json(), deduplicated=False)
        ticket = TenantTicket.query.one()
        self.assertIsNone(ticket.intake_idempotency_hash)
        self.assertRegex(ticket.intake_payload_hash, r"^[0-9a-f]{64}$")
        self.assertEqual(ticket.claim_receipt_secret_version, "v1")

    def test_missing_or_weak_dedicated_secret_fails_closed_before_persisting(self):
        for configured in ("", "too-short"):
            with self.subTest(configured=configured):
                self.app.config["TENANT_CLAIM_RECEIPT_SECRET_V1"] = configured
                response = self._post(key=f"claim-intake-secret-{len(configured):04d}")
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.get_json()["reason_code"], "claim_receipt_unavailable")
                self.assertEqual(TenantTicket.query.count(), 0)
        self.app.config["TENANT_CLAIM_RECEIPT_SECRET_V1"] = TEST_RECEIPT_SECRET

    def test_coordinates_extras_and_text_are_rejected_before_insert(self):
        invalid_payloads = [
            {"descripcion": "x", "lat": math.nan},
            {"descripcion": "x", "lat": math.inf},
            {"descripcion": "x", "lat": -90.0001},
            {"descripcion": "x", "lat": 90.0001},
            {"descripcion": "x", "lng": -180.0001},
            {"descripcion": "x", "lng": 180.0001},
            {"descripcion": "x", "lat": True},
            {"descripcion": "x", "metadata": ["not", "an", "object"]},
            {"descripcion": "x", "extras": {"tracking_pin": "123456"}},
            {"descripcion": "texto\x00invalido"},
            {"descripcion": "texto\ud800invalido"},
            {"descripcion": "x", "metadata": {"note": "valor\x00invalido"}},
            {"descripcion": "x", "metadata": {"clave\x00invalida": "valor"}},
            {"descripcion": 123},
            {"descripcion": "x", "categoria": 123},
        ]
        for index, payload in enumerate(invalid_payloads):
            # Keep deliberately invalid Unicode inside the HTTP payload only.
            # pytest-xdist/execnet serializes subtest labels as UTF-8 and cannot
            # transport an unpaired surrogate in the report metadata.
            with self.subTest(case_index=index):
                response = self._post(payload, key=f"claim-invalid-{index:04d}")
                self.assertEqual(response.status_code, 400)
        self.assertEqual(TenantTicket.query.count(), 0)

    def _existing_idempotent_ticket(self, *, key, payload):
        normalized = normalize_tenant_claim_payload(payload)
        secret = tenant_claim_receipt_secret()
        ticket = TenantTicket(
            tenant_id=self.tenant.id,
            categoria=normalized.categoria,
            descripcion=normalized.descripcion,
            datos_extra=normalized.extras,
            origen="pwa",
            latitud=normalized.latitud,
            longitud=normalized.longitud,
            intake_idempotency_hash=tenant_claim_idempotency_hash(
                tenant_id=self.tenant.id,
                key=key,
                secret=secret,
            ),
            intake_payload_hash=normalized.payload_hash,
            claim_receipt_secret_version=TENANT_CLAIM_RECEIPT_SECRET_VERSION,
        )
        db.session.add(ticket)
        db.session.commit()
        return ticket

    def test_unique_race_recovers_as_replay_instead_of_500(self):
        key = "claim-intake-race-replay-0001"
        payload = {"descripcion": "Reclamo concurrente", "categoria": "calle"}
        winner = self._existing_idempotent_ticket(key=key, payload=payload)
        error = IntegrityError("insert tenant_ticket", {}, RuntimeError("unique"))

        with patch(
            "routes.pwa_app._find_ticket_by_intake_hash",
            side_effect=[None, winner],
        ), patch.object(db.session, "commit", side_effect=error):
            response = self._post(payload, key=key)

        self.assertEqual(response.status_code, 200)
        self.assertIs(response.get_json()["deduplicated"], True)
        self.assertEqual(response.get_json()["claim"]["id"], winner.id)
        self.assertEqual(TenantTicket.query.count(), 1)

    def test_unique_race_recovers_as_conflict_for_different_payload(self):
        key = "claim-intake-race-conflict-0001"
        winner = self._existing_idempotent_ticket(
            key=key,
            payload={"descripcion": "Ganador"},
        )
        error = IntegrityError("insert tenant_ticket", {}, RuntimeError("unique"))

        with patch(
            "routes.pwa_app._find_ticket_by_intake_hash",
            side_effect=[None, winner],
        ), patch.object(db.session, "commit", side_effect=error):
            response = self._post({"descripcion": "Perdedor"}, key=key)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["reason_code"], "idempotency_key_conflict")
        self.assertEqual(TenantTicket.query.count(), 1)

    def _create_receipt_for_tracking(self):
        response = self._post(
            {
                "descripcion": "Poste inclinado",
                "categoria": "alumbrado",
                "lat": -34.6,
                "lng": -58.4,
            },
            key="claim-intake-tracking-0001",
        )
        self.assertEqual(response.status_code, 201)
        # Internal audit metadata is now server-owned, never public intake input.
        ticket = TenantTicket.query.one()
        ticket.datos_extra = {**(ticket.datos_extra or {}), "comments": [{"message": "no publicar"}]}
        db.session.commit()
        return response.get_json()

    def test_tenant_tracking_accepts_only_x_tracking_pin_and_does_not_echo_secrets(self):
        receipt = self._create_receipt_for_tracking()
        code = receipt["claim"]["code"]
        pin = receipt["access"]["pin"]
        response = self.client.get(
            f"/api/public/tracking/experience?kind=claim&code={code}&pin=wrong",
            headers={"X-Tracking-Pin": pin},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["contract_version"], "tracking.experience.v1")
        self.assertEqual(payload["resource"]["code"], code)
        self.assertEqual(payload["resource"]["source_model"], "TenantTicket")
        self.assertIs(payload["support"]["enabled"], False)
        self.assertEqual(payload["attachments"], [])
        self.assertFalse(any(item.get("type") == "comment" for item in payload["timeline"]))
        serialized = json.dumps(payload, sort_keys=True)
        self.assertNotIn(pin, serialized)
        self.assertNotIn("no publicar", serialized)
        self.assertNotIn("intake_payload_hash", serialized)
        self.assertNotIn("intake_idempotency_hash", serialized)

    def test_tenant_tracking_wrong_missing_or_query_pin_are_indistinguishable_404(self):
        receipt = self._create_receipt_for_tracking()
        code = receipt["claim"]["code"]
        pin = receipt["access"]["pin"]
        wrong = f"{(int(pin) + 1) % 1_000_000:06d}"

        responses = [
            self.client.get(
                f"/api/public/tracking/experience?kind=claim&code={code}",
                headers={"X-Tracking-Pin": wrong},
            ),
            self.client.get(f"/api/public/tracking/experience?kind=claim&code={code}"),
            self.client.get(
                f"/api/public/tracking/experience?kind=claim&code={code}&pin={pin}"
            ),
            self.client.get(
                f"/api/public/tracking/experience?kind=claim&code={code}",
                headers={"pin": pin},
            ),
        ]
        normalized_bodies = []
        for response in responses:
            self.assertEqual(response.status_code, 404)
            body = response.get_json()
            body.pop("request_id", None)
            normalized_bodies.append(body)
        self.assertTrue(all(body == normalized_bodies[0] for body in normalized_bodies[1:]))

    def test_tenant_tracking_oversized_codes_are_same_404_and_never_overflow(self):
        codes = [
            "T-2147483648",
            "T-99999999999999999999",
            "T-" + ("9" * 200),
        ]
        responses = [
            self.client.get(
                f"/api/public/tracking/experience?kind=claim&code={code}",
                headers={"X-Tracking-Pin": "123456"},
            )
            for code in codes
        ]
        normalized = []
        for response in responses:
            self.assertEqual(response.status_code, 404)
            body = response.get_json()
            body.pop("request_id", None)
            normalized.append(body)
        self.assertTrue(all(body == normalized[0] for body in normalized[1:]))
        self.assertEqual(normalized[0]["reason_code"], "claim_not_found")

    def test_tenant_tracking_never_falls_back_to_municipio_ticket(self):
        legacy = MunicipioTicket(
            tenant_id=self.tenant.id,
            municipio_id=self.owner.id,
            nro_ticket="T-987654",
            consulta_pin="123456",
            pregunta="Legacy collision",
        )
        db.session.add(legacy)
        db.session.commit()

        response = self.client.get(
            "/api/public/tracking/experience?kind=claim&code=T-987654",
            headers={"X-Tracking-Pin": "123456"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["reason_code"], "claim_not_found")

        html_response = self.client.get("/tracking/claim/T-987654?pin=123456")
        self.assertEqual(html_response.status_code, 404)

        # Existing M/S behavior remains query-PIN compatible.
        legacy.nro_ticket = "legacy-987654"
        db.session.commit()
        legacy_response = self.client.get(
            "/api/public/tracking/experience?kind=claim&code=M-legacy-987654&pin=123456"
        )
        self.assertEqual(legacy_response.status_code, 200)
        self.assertEqual(legacy_response.get_json()["resource"]["code"], "M-legacy-987654")

    def test_tenant_tracking_fails_closed_when_versioned_secret_disappears(self):
        receipt = self._create_receipt_for_tracking()
        self.app.config["TENANT_CLAIM_RECEIPT_SECRET_V1"] = ""
        response = self.client.get(
            (
                "/api/public/tracking/experience?kind=claim&code="
                f"{receipt['claim']['code']}"
            ),
            headers={"X-Tracking-Pin": receipt["access"]["pin"]},
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["reason_code"], "tracking_unavailable")


if __name__ == "__main__":
    unittest.main()
