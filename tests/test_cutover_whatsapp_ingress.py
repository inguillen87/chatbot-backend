from __future__ import annotations

import base64
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, func, select, update
from twilio.request_validator import RequestValidator
from werkzeug.datastructures import MultiDict

from cutover_ingress.app import create_cutover_ingress_app
from cutover_ingress.core import (
    CutoverIngressConfigurationError,
    CutoverIngressIntegrityError,
    CutoverIngressReplayError,
    CutoverIngressSettings,
    CutoverIngressStore,
    replay_buffered_claim,
    replay_next_into_chatboc_intake,
)
from cutover_ingress.migration import (
    CUTOVER_INGRESS_SCHEMA_REVISION,
    CutoverIngressMigrationError,
    migrate_cutover_ingress,
)
from cutover_ingress.schema import buffered_whatsapp_ingress, schema_revision
from cutover_ingress.twilio_signature import TwilioInboundSignatureValidator


BASE_TIME = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
AUTH_TOKEN = "auth-token-for-isolated-buffer-tests"
ACCOUNT_SID = "AC" + "a" * 32
EXPECTED_TO = "whatsapp:+17432643718"
PUBLIC_URL = "https://ingress.example.test/webhook/whatsapp"
POOLED_INGRESS_URL = (
    "postgresql+psycopg://ingress:secret@ep-ingress-pooler.example.test/"
    "cutover_ingress?sslmode=require"
)


def _runtime_environment(database_url: str = POOLED_INGRESS_URL) -> dict[str, str]:
    return {
        "CUTOVER_INGRESS_DATABASE_URL": database_url,
        "CUTOVER_INGRESS_PUBLIC_WEBHOOK_URL": PUBLIC_URL,
        "CUTOVER_INGRESS_TWILIO_AUTH_TOKEN": AUTH_TOKEN,
        "CUTOVER_INGRESS_TWILIO_ACCOUNT_SID": ACCOUNT_SID,
        "CUTOVER_INGRESS_EXPECTED_TO": EXPECTED_TO,
        "CUTOVER_INGRESS_TENANT_ID": "17",
        "WHATSAPP_INBOUND_HASH_SECRET": "s" * 32,
        "CUTOVER_INGRESS_STREAM_SECRET_SHA256": hashlib.sha256(
            b"s" * 32
        ).hexdigest(),
        "CUTOVER_INGRESS_ACTIVE_ENCRYPTION_KEY_ID": "active",
        "CUTOVER_INGRESS_ENCRYPTION_KEYS_JSON": json.dumps(
            {"active": _b64(b"e" * 32)}
        ),
        "CUTOVER_INGRESS_HMAC_KEY_B64": _b64(b"h" * 32),
    }


def test_ingress_http_import_does_not_load_app_providers_or_llm():
    project_root = Path(__file__).resolve().parents[1]
    script = (
        "import sys; import cutover_ingress.app; "
        "blocked=['app','routes.whatsapp_webhook','services.llm_bridge',"
        "'services.logic','twilio.rest','models','database']; "
        "assert not [name for name in blocked if name in sys.modules]"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _settings(database_url: str) -> CutoverIngressSettings:
    return CutoverIngressSettings(
        database_url=database_url,
        public_webhook_url=PUBLIC_URL,
        twilio_auth_token=AUTH_TOKEN,
        twilio_account_sid=ACCOUNT_SID,
        expected_to=EXPECTED_TO,
        tenant_id=17,
        stream_hash_secret=b"s" * 32,
        active_encryption_key_id="key-2026-08",
        encryption_keys={"key-2026-08": b"e" * 32},
        envelope_hmac_key=b"h" * 32,
        max_attempts=3,
        lease_seconds=60,
    )


def _payload(sid_suffix: str = "1", *, body: str = "Hay una luminaria caída") -> dict[str, str]:
    sid = "SM" + sid_suffix.rjust(32, "0")
    return {
        "AccountSid": ACCOUNT_SID,
        "MessageSid": sid,
        "SmsMessageSid": sid,
        "From": "whatsapp:+5492615550101",
        "To": EXPECTED_TO,
        "Body": body,
        "NumMedia": "0",
    }


@pytest.fixture()
def ingress(tmp_path):
    database_path = tmp_path / "cutover-ingress.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    settings = _settings(database_url)
    engine = create_engine(
        database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    migrate_cutover_ingress(engine)
    app = create_cutover_ingress_app(settings, engine=engine)
    try:
        yield settings, engine, app, app.extensions["cutover_ingress_store"]
    finally:
        engine.dispose()


def _signed_headers(payload: dict[str, str]) -> dict[str, str]:
    signature = RequestValidator(AUTH_TOKEN).compute_signature(PUBLIC_URL, payload)
    return {"X-Twilio-Signature": signature}


def test_environment_requires_a_distinct_database(tmp_path):
    database_url = POOLED_INGRESS_URL
    env = _runtime_environment(database_url)
    env["DATABASE_URL"] = database_url
    with pytest.raises(
        CutoverIngressConfigurationError,
        match="cutover_ingress_database_must_be_separate",
    ):
        CutoverIngressSettings.from_environ(env)


def test_environment_accepts_rotation_keyring_and_separate_database(tmp_path):
    primary = (
        "postgresql+psycopg://app:secret@ep-primary.example.test/"
        "chatboc?sslmode=require"
    )
    ingress_url = POOLED_INGRESS_URL
    env = _runtime_environment(ingress_url)
    env.update(
        DATABASE_URL=primary,
        CUTOVER_INGRESS_ACTIVE_ENCRYPTION_KEY_ID="new",
        CUTOVER_INGRESS_ENCRYPTION_KEYS_JSON=json.dumps(
            {"old": _b64(b"o" * 32), "new": _b64(b"n" * 32)}
        ),
    )
    settings = CutoverIngressSettings.from_environ(env)
    assert settings.database_url == ingress_url
    assert settings.active_encryption_key_id == "new"
    assert settings.encryption_keys["old"] == b"o" * 32


def test_environment_normalizes_standard_neon_url_to_bundled_psycopg_driver():
    raw_url = POOLED_INGRESS_URL.replace("postgresql+psycopg://", "postgresql://")
    settings = CutoverIngressSettings.from_environ(_runtime_environment(raw_url))

    assert settings.database_url.startswith("postgresql+psycopg://")


def test_environment_rejects_stale_redacted_stream_secret_fingerprint(tmp_path):
    env = _runtime_environment()
    env["CUTOVER_INGRESS_STREAM_SECRET_SHA256"] = "0" * 64
    with pytest.raises(
        CutoverIngressConfigurationError,
        match="cutover_ingress_stream_secret_fingerprint_mismatch",
    ):
        CutoverIngressSettings.from_environ(env)


@pytest.mark.parametrize(
    ("database_url", "error_code"),
    [
        (
            "sqlite:///:memory:",
            "cutover_ingress_runtime_database_must_be_postgresql",
        ),
        (
            "postgresql+psycopg://ingress:secret@ep-ingress.example.test/"
            "cutover_ingress?sslmode=require",
            "cutover_ingress_runtime_database_must_be_pooled",
        ),
        (
            "postgresql+psycopg://ingress:secret@ep-ingress-pooler.example.test/"
            "cutover_ingress",
            "cutover_ingress_database_tls_required",
        ),
    ],
)
def test_environment_runtime_rejects_ephemeral_direct_or_non_tls_database(
    database_url,
    error_code,
):
    with pytest.raises(CutoverIngressConfigurationError, match=error_code):
        CutoverIngressSettings.from_environ(_runtime_environment(database_url))


@pytest.mark.parametrize(
    "target_override",
    [
        "host=attacker.example",
        "hostaddr=203.0.113.10",
        "port=5433",
        "dbname=other",
        "database=other",
        "service=other",
        "options=-csearch_path%3Dother",
        "search_path=other",
        "target_session_attrs=read-write",
    ],
)
def test_environment_rejects_query_parameters_that_can_override_database_target(
    target_override,
):
    database_url = POOLED_INGRESS_URL + "&" + target_override
    with pytest.raises(
        CutoverIngressConfigurationError,
        match="cutover_ingress_database_target_options_forbidden",
    ):
        CutoverIngressSettings.from_environ(_runtime_environment(database_url))


def test_wsgi_factory_rejects_sqlite_environment_before_app_can_ack(tmp_path):
    sqlite_url = f"sqlite:///{(tmp_path / 'must-not-ack.sqlite3').as_posix()}"
    with patch.dict("os.environ", _runtime_environment(sqlite_url), clear=True):
        with pytest.raises(
            CutoverIngressConfigurationError,
            match="cutover_ingress_runtime_database_must_be_postgresql",
        ):
            create_cutover_ingress_app()
    assert not (tmp_path / "must-not-ack.sqlite3").exists()


def test_migration_records_exact_revision_and_refuses_nonempty_unknown_database(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'schema.sqlite3').as_posix()}")
    assert migrate_cutover_ingress(engine) == CUTOVER_INGRESS_SCHEMA_REVISION
    assert migrate_cutover_ingress(engine) == CUTOVER_INGRESS_SCHEMA_REVISION
    with engine.connect() as connection:
        assert connection.scalar(select(schema_revision.c.revision)) == (
            CUTOVER_INGRESS_SCHEMA_REVISION
        )
    engine.dispose()

    unknown = create_engine(f"sqlite:///{(tmp_path / 'unknown.sqlite3').as_posix()}")
    with unknown.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    with pytest.raises(
        CutoverIngressMigrationError,
        match="cutover_ingress_database_not_empty",
    ):
        migrate_cutover_ingress(unknown)
    unknown.dispose()


def test_missing_partial_stream_lease_index_fails_preflight_and_startup(tmp_path):
    database_url = f"sqlite:///{(tmp_path / 'missing-index.sqlite3').as_posix()}"
    engine = create_engine(database_url, future=True)
    migrate_cutover_ingress(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "DROP INDEX uq_cutover_whatsapp_ingress_stream_replaying"
        )

    with pytest.raises(
        CutoverIngressMigrationError,
        match="cutover_ingress_schema_indexes_mismatch",
    ):
        migrate_cutover_ingress(engine)
    with pytest.raises(
        CutoverIngressMigrationError,
        match="cutover_ingress_schema_indexes_mismatch",
    ):
        create_cutover_ingress_app(_settings(database_url), engine=engine)
    engine.dispose()


def test_webhook_rejects_unsigned_or_invalid_signature_before_persistence(ingress):
    _, engine, app, _ = ingress
    payload = _payload()
    with app.test_client() as client:
        assert client.post("/webhook/whatsapp", data=payload).status_code == 403
        assert (
            client.post(
                "/webhook/whatsapp",
                data=payload,
                headers={"X-Twilio-Signature": "forged"},
            ).status_code
            == 403
        )
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(buffered_whatsapp_ingress)) == 0


def test_webhook_matches_twilio_default_https_port_fallback(ingress):
    _, _, app, store = ingress
    payload = _payload("80")
    signature = RequestValidator(AUTH_TOKEN).compute_signature(
        "https://ingress.example.test:443/webhook/whatsapp",
        payload,
    )
    with app.test_client() as client:
        response = client.post(
            "/webhook/whatsapp",
            data=payload,
            headers={"X-Twilio-Signature": signature},
        )
    assert response.status_code == 200
    assert store.get_record(payload["MessageSid"]) is not None


def test_local_signature_validator_matches_official_mixed_case_port_fallback():
    payload = _payload("83")
    configured_url = "https://Ingress.Example.test:443/webhook/whatsapp"
    signed_url = "https://Ingress.Example.test/webhook/whatsapp"
    signature = RequestValidator(AUTH_TOKEN).compute_signature(signed_url, payload)

    assert RequestValidator(AUTH_TOKEN).validate(
        configured_url,
        payload,
        signature,
    )
    assert TwilioInboundSignatureValidator(AUTH_TOKEN).validate(
        configured_url,
        payload,
        signature,
    )


@pytest.mark.parametrize(
    "public_url",
    [
        "https://Ingress.Example.test/webhook/whatsapp",
        "https://ingress.example.test:443/webhook/whatsapp",
        "https://user@ingress.example.test/webhook/whatsapp",
        "https://[2001:db8::1]/webhook/whatsapp",
    ],
)
def test_settings_rejects_noncanonical_public_webhook_url(public_url, tmp_path):
    settings = _settings(f"sqlite:///{(tmp_path / 'canonical.sqlite3').as_posix()}")
    with pytest.raises(
        CutoverIngressConfigurationError,
        match="cutover_ingress_public_webhook_url_invalid",
    ):
        replace(settings, public_webhook_url=public_url)


def test_webhook_matches_twilio_form_encoding_for_unicode_reserved_and_empty_values(
    ingress,
):
    _, _, app, store = ingress
    payload = _payload(
        "81",
        body="Árbol caído & semáforo + ubicación=/Centro? referencia #1",
    )
    payload.update(
        {
            "ProfileName": "Vecina de Junín",
            "WaId": "+5492615550101",
            "ReferralBody": "",
        }
    )
    with app.test_client() as client:
        response = client.post(
            "/webhook/whatsapp",
            data=payload,
            headers=_signed_headers(payload),
            content_type="application/x-www-form-urlencoded",
        )
    assert response.status_code == 200
    assert store.get_record(payload["MessageSid"]) is not None


def test_webhook_rejects_signed_duplicate_form_keys_as_documented_fail_closed_exception(
    ingress,
):
    _, engine, app, _ = ingress
    payload = _payload("82")
    form = MultiDict(payload.items())
    form.add("Body", "segundo valor ambiguo")
    signature = RequestValidator(AUTH_TOKEN).compute_signature(PUBLIC_URL, form)
    with app.test_client() as client:
        response = client.post(
            "/webhook/whatsapp",
            data=form,
            headers={"X-Twilio-Signature": signature},
        )
    assert response.status_code == 422
    with engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(buffered_whatsapp_ingress)
        ) == 0


def test_webhook_persists_encrypted_before_ack_and_exact_duplicate_is_idempotent(ingress):
    _, engine, app, store = ingress
    payload = _payload(body="Mi domicilio privado no debe quedar en claro")
    with app.test_client() as client:
        first = client.post(
            "/webhook/whatsapp",
            data=payload,
            headers=_signed_headers(payload),
        )
        duplicate = client.post(
            "/webhook/whatsapp",
            data=payload,
            headers=_signed_headers(payload),
        )
    assert first.status_code == duplicate.status_code == 200
    assert first.mimetype == "application/xml"
    assert first.headers["Cache-Control"] == "no-store"
    row = store.get_record(payload["MessageSid"])
    assert row is not None
    assert row["status"] == "buffered"
    assert row["nonce"] and len(row["nonce"]) == 12
    assert b"domicilio privado" not in bytes(row["ciphertext"])
    assert len(row["envelope_hmac"]) == 64
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(buffered_whatsapp_ingress)) == 1


def test_stream_key_matches_canonical_backend_derivation(ingress):
    from services.whatsapp_inbound_turns import derive_whatsapp_stream_key

    settings, _, _, store = ingress
    payload = _payload("7")
    receipt = store.persist(payload, now=BASE_TIME)
    canonical = derive_whatsapp_stream_key(
        secret=settings.stream_hash_secret,
        tenant_id=settings.tenant_id,
        sender=payload["From"],
        destination=payload["To"],
    )
    assert receipt.stream_key == canonical


def test_http_ack_is_emitted_only_after_committed_row_is_visible(ingress):
    _, engine, app, store = ingress
    payload = _payload("8")
    original_persist = store.persist
    commit_observed_before_return = False

    def persist_and_verify_commit(form):
        nonlocal commit_observed_before_return
        receipt = original_persist(form, now=BASE_TIME)
        # A distinct transaction can only see the row after Store.persist's
        # transaction committed.  The Flask handler cannot ACK before this
        # wrapper returns.
        with engine.connect() as connection:
            commit_observed_before_return = (
                connection.scalar(
                    select(func.count())
                    .select_from(buffered_whatsapp_ingress)
                    .where(
                        buffered_whatsapp_ingress.c.message_sid
                        == payload["MessageSid"]
                    )
                )
                == 1
            )
        return receipt

    with patch.object(store, "persist", side_effect=persist_and_verify_commit):
        with app.test_client() as client:
            response = client.post(
                "/webhook/whatsapp",
                data=payload,
                headers=_signed_headers(payload),
            )
    assert response.status_code == 200
    assert commit_observed_before_return is True


def test_same_message_sid_with_changed_signed_payload_fails_closed(ingress):
    _, _, app, _ = ingress
    original = _payload(body="Mensaje original")
    changed = _payload(body="Mensaje alterado")
    with app.test_client() as client:
        assert client.post(
            "/webhook/whatsapp", data=original, headers=_signed_headers(original)
        ).status_code == 200
        conflict = client.post(
            "/webhook/whatsapp", data=changed, headers=_signed_headers(changed)
        )
    assert conflict.status_code == 409


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("AccountSid", "AC" + "b" * 32),
        ("To", "whatsapp:+17432640000"),
    ],
)
def test_signed_but_wrong_provider_scope_is_rejected(ingress, field, value):
    _, _, app, _ = ingress
    payload = _payload()
    payload[field] = value
    with app.test_client() as client:
        response = client.post(
            "/webhook/whatsapp", data=payload, headers=_signed_headers(payload)
        )
    assert response.status_code == 422


@dataclass(frozen=True)
class _TargetReceipt:
    outcome: str
    turn_id: str
    tenant_id: int
    provider_message_sid: str
    stream_key: str


def _target_receipt(claim, outcome="created"):
    return _TargetReceipt(
        outcome=outcome,
        turn_id="11111111-1111-4111-8111-111111111111",
        tenant_id=claim.tenant_id,
        provider_message_sid=claim.message_sid,
        stream_key=claim.stream_key,
    )


def test_fifo_claim_blocks_next_stream_item_until_first_is_replayed(ingress):
    _, _, _, store = ingress
    store.persist(_payload("1"), now=BASE_TIME)
    store.persist(_payload("2"), now=BASE_TIME + timedelta(seconds=1))
    first = store.claim_next(now=BASE_TIME + timedelta(seconds=2))
    assert first and first.message_sid == _payload("1")["MessageSid"]
    assert store.claim_next(now=BASE_TIME + timedelta(seconds=2)) is None

    calls = []

    def ingest(**kwargs):
        calls.append(kwargs)
        return _target_receipt(first)

    replay_buffered_claim(
        store,
        first,
        ingest,
        now=BASE_TIME + timedelta(seconds=2),
    )
    second = store.claim_next(now=BASE_TIME + timedelta(seconds=3))
    assert second and second.message_sid == _payload("2")["MessageSid"]
    assert calls[0]["received_at"] == BASE_TIME
    assert calls[0]["provider"] == "twilio"


def test_crash_after_target_commit_replays_safely_as_target_duplicate(ingress):
    _, _, _, store = ingress
    store.persist(_payload("3"), now=BASE_TIME)
    first = store.claim_next(now=BASE_TIME + timedelta(seconds=1))
    assert first
    target_calls = []

    def target_created(**kwargs):
        target_calls.append(kwargs)
        return _target_receipt(first, "created")

    with patch.object(store, "complete_claim", side_effect=RuntimeError("simulated crash")):
        with pytest.raises(CutoverIngressReplayError, match="target_ingest_failed"):
            replay_buffered_claim(
                store,
                first,
                target_created,
                now=BASE_TIME + timedelta(seconds=1),
            )

    second = store.claim_next(now=BASE_TIME + timedelta(seconds=17))
    assert second and second.attempt_count == 2

    def target_duplicate(**kwargs):
        target_calls.append(kwargs)
        return _target_receipt(second, "duplicate")

    replay_buffered_claim(
        store,
        second,
        target_duplicate,
        now=BASE_TIME + timedelta(seconds=17),
    )
    row = store.get_record(second.message_sid)
    assert row["status"] == "replayed"
    assert row["target_outcome"] == "duplicate"
    assert len(target_calls) == 2


def test_tampered_ciphertext_is_quarantined_without_target_replay(ingress):
    _, engine, _, store = ingress
    payload = _payload("4")
    store.persist(payload, now=BASE_TIME)
    with engine.begin() as connection:
        connection.execute(
            update(buffered_whatsapp_ingress)
            .where(buffered_whatsapp_ingress.c.message_sid == payload["MessageSid"])
            .values(ciphertext=b"tampered")
        )
    with pytest.raises(CutoverIngressIntegrityError):
        store.claim_next(now=BASE_TIME + timedelta(seconds=1))
    row = store.get_record(payload["MessageSid"])
    assert row["status"] == "dead"
    assert row["last_error_code"] == "encrypted_envelope_invalid"


def test_claim_hmac_tampering_never_calls_target(ingress):
    _, _, _, store = ingress
    store.persist(_payload("5"), now=BASE_TIME)
    claim = store.claim_next(now=BASE_TIME + timedelta(seconds=1))
    assert claim
    forged = replace(claim, claim_hmac="0" * 64)
    called = False

    def ingest(**kwargs):
        nonlocal called
        called = True

    with pytest.raises(CutoverIngressIntegrityError, match="claim_hmac_invalid"):
        replay_buffered_claim(
            store,
            forged,
            ingest,
            now=BASE_TIME + timedelta(seconds=1),
        )
    assert called is False


def test_replay_bridge_writes_only_the_real_durable_intake(
    ingress,
    tmp_path,
    monkeypatch,
):
    from flask import Flask
    from sqlalchemy.orm import Session

    from database import db
    from models import WhatsAppInboundTurn
    from services import global_writer_authority as writer_authority

    _, _, _, store = ingress
    store.persist(_payload("6"), now=BASE_TIME)

    target_app = Flask("cutover-target-intake-test")
    target_app.config.update(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI=(
            f"sqlite:///{(tmp_path / 'target.sqlite3').as_posix()}"
        ),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"check_same_thread": False}},
        WHATSAPP_INBOUND_HASH_SECRET="s" * 32,
        CUTOVER_GLOBAL_WRITER_AUTHORITY_ENABLED=True,
        CUTOVER_RUNTIME_IDENTITY="vercel",
    )
    monkeypatch.setattr(
        writer_authority,
        "evaluate_global_writer_authority",
        lambda _config: writer_authority.GlobalWriterAuthorityDecision(
            allowed=True,
            enabled=True,
            reason_code="runtime_is_global_writer_owner",
            epoch=8,
        ),
    )
    db.init_app(target_app)
    with target_app.app_context():
        WhatsAppInboundTurn.__table__.create(db.engine)
        try:
            receipt = replay_next_into_chatboc_intake(
                store,
                now=BASE_TIME + timedelta(seconds=1),
            )
            assert receipt is not None and receipt.created is True
            with Session(db.engine) as session:
                target = session.scalar(select(WhatsAppInboundTurn))
                assert target.provider_message_sid == _payload("6")["MessageSid"]
                assert target.status == "received"
                assert target.payload_json["Body"] == "Hay una luminaria caída"
                assert target.received_at.replace(tzinfo=timezone.utc) == BASE_TIME
        finally:
            db.session.remove()
            WhatsAppInboundTurn.__table__.drop(db.engine)
            db.engine.dispose()

    buffered = store.get_record(_payload("6")["MessageSid"])
    assert buffered["status"] == "replayed"
    assert buffered["target_turn_id"] == receipt.turn_id


def test_replay_bridge_refuses_target_with_different_stream_secret_before_lease(
    ingress,
    monkeypatch,
):
    from flask import Flask
    from services import global_writer_authority as writer_authority

    _, _, _, store = ingress
    payload = _payload("9")
    store.persist(payload, now=BASE_TIME)
    target_app = Flask("wrong-cutover-target-secret")
    target_app.config.update(
        WHATSAPP_INBOUND_HASH_SECRET="different" * 4,
        CUTOVER_GLOBAL_WRITER_AUTHORITY_ENABLED=True,
        CUTOVER_RUNTIME_IDENTITY="vercel",
    )
    monkeypatch.setattr(
        writer_authority,
        "evaluate_global_writer_authority",
        lambda _config: writer_authority.GlobalWriterAuthorityDecision(
            allowed=True,
            enabled=True,
            reason_code="runtime_is_global_writer_owner",
            epoch=8,
        ),
    )
    with target_app.app_context(), pytest.raises(
        CutoverIngressConfigurationError,
        match="cutover_ingress_target_stream_secret_mismatch",
    ):
        replay_next_into_chatboc_intake(
            store,
            now=BASE_TIME + timedelta(seconds=1),
        )
    row = store.get_record(payload["MessageSid"])
    assert row["status"] == "buffered"
    assert row["attempt_count"] == 0


def test_replay_bridge_requires_target_global_writer_ownership_before_lease(
    ingress,
    monkeypatch,
):
    from flask import Flask
    from services import global_writer_authority as writer_authority

    _, _, _, store = ingress
    payload = _payload("10")
    store.persist(payload, now=BASE_TIME)
    target_app = Flask("non-owner-cutover-target")
    target_app.config.update(
        WHATSAPP_INBOUND_HASH_SECRET="s" * 32,
        CUTOVER_GLOBAL_WRITER_AUTHORITY_ENABLED=True,
        CUTOVER_RUNTIME_IDENTITY="render",
    )
    monkeypatch.setattr(
        writer_authority,
        "evaluate_global_writer_authority",
        lambda _config: writer_authority.GlobalWriterAuthorityDecision(
            allowed=False,
            enabled=True,
            reason_code="runtime_not_global_writer_owner",
            epoch=9,
        ),
    )
    with target_app.app_context(), pytest.raises(
        CutoverIngressConfigurationError,
        match="cutover_ingress_global_writer_authority_denied",
    ):
        replay_next_into_chatboc_intake(
            store,
            now=BASE_TIME + timedelta(seconds=1),
        )
    row = store.get_record(payload["MessageSid"])
    assert row["status"] == "buffered"
    assert row["attempt_count"] == 0
