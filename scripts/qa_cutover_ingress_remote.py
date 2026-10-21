"""Signed remote canary for the isolated WhatsApp cutover ingress.

The canary never calls Twilio and never replays into Chatboc. It signs a
synthetic form locally, verifies durable encrypted persistence through the
pooled runtime database, and removes only its exact synthetic rows.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
import uuid

from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool
from twilio.request_validator import RequestValidator


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cutover_ingress.schema import buffered_whatsapp_ingress


def _required(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"missing_required_environment:{name}")
    return value


def _psycopg_url(value: str) -> str:
    url = make_url(value)
    if url.get_backend_name().lower() != "postgresql":
        raise RuntimeError("canary_database_must_be_postgresql")
    return url.set(drivername="postgresql+psycopg").render_as_string(
        hide_password=False
    )


def _post(url: str, payload: dict[str, str], signature: str) -> int:
    request = Request(
        url,
        data=urlencode(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Twilio-Signature": signature,
            "User-Agent": "chatboc-cutover-ingress-canary/1",
        },
        method="POST",
    )
    try:
        with urlopen(
            request,
            timeout=20,
            context=ssl.create_default_context(),
        ) as response:
            response.read(4096)
            return int(response.status)
    except HTTPError as exc:
        exc.read(4096)
        return int(exc.code)


def main() -> int:
    webhook_url = _required("CUTOVER_INGRESS_PUBLIC_WEBHOOK_URL")
    parsed = urlsplit(webhook_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise RuntimeError("canary_webhook_url_invalid")
    auth_token = _required("CUTOVER_INGRESS_TWILIO_AUTH_TOKEN")
    account_sid = _required("CUTOVER_INGRESS_TWILIO_ACCOUNT_SID")
    expected_to = _required("CUTOVER_INGRESS_EXPECTED_TO")
    tenant_id = int(_required("CUTOVER_INGRESS_TENANT_ID"))
    database_url = _psycopg_url(_required("CUTOVER_INGRESS_DATABASE_URL"))

    message_sid = "SM" + uuid.uuid4().hex
    invalid_sid = "SM" + uuid.uuid4().hex
    body = "CANARY SINTETICO - luminaria de prueba, sin persona ni domicilio real"
    payload = {
        "AccountSid": account_sid,
        "MessageSid": message_sid,
        "SmsMessageSid": message_sid,
        "From": "whatsapp:+5492610000000",
        "To": expected_to,
        "Body": body,
        "NumMedia": "0",
    }
    validator = RequestValidator(auth_token)
    signature = validator.compute_signature(webhook_url, payload)
    changed = dict(payload, Body=body + " alterado")
    changed_signature = validator.compute_signature(webhook_url, changed)
    invalid = dict(payload, MessageSid=invalid_sid, SmsMessageSid=invalid_sid)

    engine = create_engine(
        database_url,
        future=True,
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={
            "application_name": "chatboc_cutover_ingress_remote_canary",
            "connect_timeout": 10,
        },
    )
    statuses: dict[str, int] = {}
    persisted = False
    encrypted = False
    exact_row_count = 0
    cleanup_count = 0
    try:
        statuses["invalid_signature"] = _post(webhook_url, invalid, "forged")
        statuses["created"] = _post(webhook_url, payload, signature)
        statuses["duplicate"] = _post(webhook_url, payload, signature)
        statuses["conflict"] = _post(webhook_url, changed, changed_signature)
        if statuses != {
            "invalid_signature": 403,
            "created": 200,
            "duplicate": 200,
            "conflict": 409,
        }:
            raise RuntimeError("remote_canary_http_contract_failed")

        with engine.connect() as connection:
            rows = list(
                connection.execute(
                    select(buffered_whatsapp_ingress).where(
                        buffered_whatsapp_ingress.c.message_sid == message_sid
                    )
                ).mappings()
            )
        exact_row_count = len(rows)
        if exact_row_count != 1:
            raise RuntimeError("remote_canary_persistence_count_failed")
        row = rows[0]
        persisted = row["status"] == "buffered" and row["tenant_id"] == tenant_id
        encrypted = body.encode("utf-8") not in bytes(row["ciphertext"])
        if not persisted or not encrypted or not row["envelope_hmac"]:
            raise RuntimeError("remote_canary_encrypted_persistence_failed")
    finally:
        with engine.begin() as connection:
            cleanup_count = int(
                connection.execute(
                    delete(buffered_whatsapp_ingress).where(
                        buffered_whatsapp_ingress.c.message_sid.in_(
                            [message_sid, invalid_sid]
                        )
                    )
                ).rowcount
                or 0
            )
        with engine.connect() as connection:
            remaining = int(
                connection.scalar(
                    select(func.count())
                    .select_from(buffered_whatsapp_ingress)
                    .where(
                        buffered_whatsapp_ingress.c.message_sid.in_(
                            [message_sid, invalid_sid]
                        )
                    )
                )
                or 0
            )
        engine.dispose()
        if remaining:
            raise RuntimeError("remote_canary_cleanup_failed")

    print(
        json.dumps(
            {
                "cleanup_rows": cleanup_count,
                "contract_version": "chatboc.cutover_ingress.remote_canary.v1",
                "database_url_exposed": False,
                "encrypted": encrypted,
                "exact_row_count": exact_row_count,
                "persisted": persisted,
                "provider_called": False,
                "replay_attempted": False,
                "status": "passed",
                "statuses": statuses,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
