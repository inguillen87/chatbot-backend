"""Collect and sign Junin's Twilio sender state using GET-only provider APIs.

There is intentionally no apply mode, message transport, POST, PATCH, PUT or
DELETE capability in this module.  The output is the strict provider snapshot
envelope consumed by ``promote_tenant_provider_connection.py``.  Credential
values are used only in memory for HTTP Basic authentication and an opaque
domain-separated binding; they are never logged or placed in the document.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlsplit, urlunsplit

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.provider_cutover_evidence import (  # noqa: E402
    CutoverEvidenceError,
    account_sid,
    clean,
    credential_binding,
    environment_name,
    evidence_id,
    require,
    required_environment_value,
    sha256_value,
    signed_envelope,
)


CONTRACT_VERSION = "twilio.read_only.connection_snapshot.v1"
TENANT_SLUG = "junin"
PROVIDER = "twilio"
CHANNEL = "whatsapp"
ENVIRONMENT = "production"
OFFICIAL_PHONE = "+17432643718"
SENDERS_URL = "https://messaging.twilio.com/v2/Channels/Senders"
SERVICES_URL = "https://messaging.twilio.com/v1/Services"
_SID_RE = re.compile(r"^XE[0-9A-Fa-f]{32}$")
_SERVICE_SID_RE = re.compile(r"^MG[0-9A-Fa-f]{32}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{7,64}$")
_VERCEL_ID_RE = re.compile(r"^(?:prj|dpl)_[A-Za-z0-9]{8,128}$")


class ReadOnlyTwilioSource(Protocol):
    account_sid: str

    def list_senders(self) -> list[Mapping[str, Any]]: ...

    def fetch_sender(self, sender_sid: str) -> Mapping[str, Any]: ...

    def list_services(self) -> list[Mapping[str, Any]]: ...

    def list_service_senders(self, service_sid: str) -> list[Mapping[str, Any]]: ...


def _https_url(value: Any, *, reason_code: str) -> str:
    rendered = clean(value)
    require(bool(rendered) and len(rendered) <= 500, reason_code)
    require(not any(ord(character) < 32 for character in rendered), reason_code)
    try:
        parsed = urlsplit(rendered)
        host = clean(parsed.hostname).lower()
        port = parsed.port
    except ValueError as exc:
        raise CutoverEvidenceError(reason_code) from exc
    require(parsed.scheme.lower() == "https", reason_code)
    require(bool(host), reason_code)
    require(parsed.username is None and parsed.password is None, reason_code)
    require(not parsed.fragment and port in (None, 443), reason_code)
    netloc = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return urlunsplit(("https", netloc, parsed.path or "/", parsed.query, ""))


def _sender_phone(payload: Mapping[str, Any]) -> str:
    raw = clean(
        payload.get("sender_id")
        or payload.get("senderId")
        or payload.get("sender")
    )
    if raw.lower().startswith("whatsapp:"):
        raw = raw.split(":", 1)[1].strip()
    return raw


def _list_payload(payload: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    rows = payload.get(key)
    require(isinstance(rows, list), "twilio_provider_list_shape_invalid")
    require(
        all(isinstance(item, Mapping) for item in rows),
        "twilio_provider_list_shape_invalid",
    )
    meta = payload.get("meta")
    require(isinstance(meta, Mapping), "twilio_provider_pagination_metadata_missing")
    require(
        not clean(meta.get("next_page_url")),
        "twilio_provider_pagination_not_exhaustive",
    )
    return list(rows)


class TwilioV2GetOnlyClient:
    """A capability-limited client exposing only four allowlisted GETs."""

    def __init__(
        self,
        *,
        account_sid_value: str,
        auth_token: str,
        session: requests.Session | None = None,
        timeout_seconds: int = 15,
    ):
        self.account_sid = account_sid(
            account_sid_value,
            reason_code="twilio_account_sid_invalid",
        )
        require(bool(auth_token), "twilio_auth_token_missing")
        self._auth_token = auth_token
        self._session = session or requests.Session()
        self._timeout_seconds = int(timeout_seconds)
        require(1 <= self._timeout_seconds <= 30, "twilio_timeout_invalid")

    def _get(self, url: str) -> Mapping[str, Any]:
        parsed = urlsplit(url)
        require(
            parsed.scheme == "https"
            and parsed.hostname == "messaging.twilio.com"
            and parsed.username is None
            and parsed.password is None
            and parsed.fragment == "",
            "twilio_read_endpoint_not_allowlisted",
        )
        allowed = (
            parsed.path == "/v2/Channels/Senders"
            or bool(re.fullmatch(r"/v2/Channels/Senders/XE[0-9A-Fa-f]{32}", parsed.path))
            or parsed.path == "/v1/Services"
            or bool(
                re.fullmatch(
                    r"/v1/Services/MG[0-9A-Fa-f]{32}/ChannelSenders",
                    parsed.path,
                )
            )
        )
        require(allowed, "twilio_read_endpoint_not_allowlisted")
        response = self._session.get(
            url,
            params={"PageSize": "1000"} if url in {SENDERS_URL, SERVICES_URL} or url.endswith("/ChannelSenders") else None,
            auth=(self.account_sid, self._auth_token),
            headers={"Accept": "application/json"},
            allow_redirects=False,
            timeout=self._timeout_seconds,
        )
        require(response.status_code == 200, "twilio_provider_read_failed")
        try:
            payload = response.json()
        except Exception as exc:
            raise CutoverEvidenceError("twilio_provider_json_invalid") from exc
        require(isinstance(payload, Mapping), "twilio_provider_json_invalid")
        return payload

    def list_senders(self) -> list[Mapping[str, Any]]:
        return _list_payload(self._get(SENDERS_URL), "senders")

    def fetch_sender(self, sender_sid: str) -> Mapping[str, Any]:
        require(bool(_SID_RE.fullmatch(sender_sid)), "twilio_sender_sid_invalid")
        return self._get(f"{SENDERS_URL}/{sender_sid}")

    def list_services(self) -> list[Mapping[str, Any]]:
        return _list_payload(self._get(SERVICES_URL), "services")

    def list_service_senders(self, service_sid: str) -> list[Mapping[str, Any]]:
        require(
            bool(_SERVICE_SID_RE.fullmatch(service_sid)),
            "twilio_messaging_service_sid_invalid",
        )
        return _list_payload(
            self._get(f"{SERVICES_URL}/{service_sid}/ChannelSenders"),
            "channel_senders",
        )


def collect_snapshot_document(
    source: ReadOnlyTwilioSource,
    *,
    tenant_id: int,
    credential_environment_variable: str,
    signing_key_environment_variable: str,
    credential_binding_key_environment_variable: str,
    credential_binding_hmac_sha256: str,
    expected_webhook_url: str,
    expected_status_callback_url: str,
    destination_project_id: str,
    destination_deployment_id: str,
    destination_deployment_revision: str,
    database_identity_sha256: str,
    challenge_nonce: str,
    cutover_window_evidence_id: str,
    evidence_id_value: str,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    now = observed_at or datetime.now(timezone.utc)
    require(now.tzinfo is not None, "provider_snapshot_clock_invalid")
    credential_env = environment_name(
        credential_environment_variable,
        reason_code="credential_environment_variable_invalid",
    )
    signing_env = environment_name(
        signing_key_environment_variable,
        reason_code="snapshot_signing_key_environment_variable_invalid",
    )
    binding_env = environment_name(
        credential_binding_key_environment_variable,
        reason_code="credential_binding_key_environment_variable_invalid",
    )
    require(
        len({credential_env, signing_env, binding_env}) == 3,
        "provider_snapshot_key_environment_variables_must_differ",
    )
    webhook = _https_url(expected_webhook_url, reason_code="expected_webhook_url_invalid")
    callback = _https_url(
        expected_status_callback_url,
        reason_code="expected_status_callback_url_invalid",
    )
    revision = clean(destination_deployment_revision).lower()
    require(bool(_REVISION_RE.fullmatch(revision)), "destination_revision_invalid")
    require(
        bool(_VERCEL_ID_RE.fullmatch(clean(destination_project_id)))
        and clean(destination_project_id).startswith("prj_"),
        "destination_project_id_invalid",
    )
    require(
        bool(_VERCEL_ID_RE.fullmatch(clean(destination_deployment_id)))
        and clean(destination_deployment_id).startswith("dpl_"),
        "destination_deployment_id_invalid",
    )
    database_fingerprint = sha256_value(
        database_identity_sha256,
        reason_code="database_identity_sha256_invalid",
    )
    binding = sha256_value(
        credential_binding_hmac_sha256,
        reason_code="credential_binding_hmac_sha256_invalid",
    )
    nonce = evidence_id(challenge_nonce, reason_code="challenge_nonce_invalid")
    window = evidence_id(
        cutover_window_evidence_id,
        reason_code="cutover_window_evidence_id_invalid",
    )
    evidence = evidence_id(evidence_id_value, reason_code="provider_snapshot_evidence_id_invalid")

    listed = source.list_senders()
    matching = [item for item in listed if _sender_phone(item) == OFFICIAL_PHONE]
    require(len(matching) == 1, "twilio_official_sender_exactly_one_required")
    sender_sid = clean(matching[0].get("sid"))
    require(bool(_SID_RE.fullmatch(sender_sid)), "twilio_sender_sid_invalid")
    sender = source.fetch_sender(sender_sid)
    require(clean(sender.get("sid")) == sender_sid, "twilio_sender_detail_sid_mismatch")
    require(_sender_phone(sender) == OFFICIAL_PHONE, "twilio_sender_detail_phone_mismatch")
    require(clean(sender.get("status")) == "ONLINE", "twilio_sender_not_online")
    webhook_state = sender.get("webhook")
    require(isinstance(webhook_state, Mapping), "twilio_sender_webhook_shape_invalid")
    observed_webhook = _https_url(
        webhook_state.get("callback_url"),
        reason_code="twilio_sender_webhook_url_invalid",
    )
    observed_callback = _https_url(
        webhook_state.get("status_callback_url"),
        reason_code="twilio_sender_status_callback_url_invalid",
    )
    require(observed_webhook == webhook, "twilio_sender_webhook_mismatch")
    require(observed_callback == callback, "twilio_sender_status_callback_mismatch")

    memberships: list[tuple[str, Mapping[str, Any]]] = []
    for service in source.list_services():
        service_sid = clean(service.get("sid"))
        require(
            bool(_SERVICE_SID_RE.fullmatch(service_sid)),
            "twilio_messaging_service_sid_invalid",
        )
        rows = source.list_service_senders(service_sid)
        if any(clean(item.get("sid")) == sender_sid for item in rows):
            memberships.append((service_sid, service))
    require(
        len(memberships) == 1,
        "twilio_sender_messaging_service_exactly_one_required",
    )
    messaging_service_sid, service = memberships[0]
    service_webhook = clean(service.get("inbound_request_url"))
    service_callback = clean(service.get("status_callback"))
    if service_webhook:
        require(
            _https_url(service_webhook, reason_code="twilio_service_webhook_invalid")
            == webhook,
            "twilio_service_webhook_mismatch",
        )
    if service_callback:
        require(
            _https_url(service_callback, reason_code="twilio_service_callback_invalid")
            == callback,
            "twilio_service_callback_mismatch",
        )

    return {
        "contract_version": CONTRACT_VERSION,
        "source_kind": "provider_api_read",
        "read_only": True,
        "mutations_performed": False,
        "messages_sent": False,
        "evidence_id": evidence,
        "observed_at": now.astimezone(timezone.utc).isoformat(),
        "tenant_slug": TENANT_SLUG,
        "tenant_id": int(tenant_id),
        "provider": PROVIDER,
        "channel": CHANNEL,
        "environment": ENVIRONMENT,
        "resource_count": 1,
        "account_sid": source.account_sid,
        "credential_account_sid": source.account_sid,
        "credential_environment_variable": credential_env,
        "signing_key_environment_variable": signing_env,
        "credential_binding_key_environment_variable": binding_env,
        "credential_binding_hmac_sha256": binding,
        "sender_count": 1,
        "sender_sid": sender_sid,
        "messaging_service_sid": messaging_service_sid,
        "phone_number": OFFICIAL_PHONE,
        "sender_status": "ONLINE",
        "webhook_url": observed_webhook,
        "status_callback_url": observed_callback,
        "destination_project_id": clean(destination_project_id),
        "destination_deployment_id": clean(destination_deployment_id),
        "destination_deployment_revision": revision,
        "database_identity_sha256": database_fingerprint,
        "challenge_nonce": nonce,
        "cutover_window_evidence_id": window,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--account-sid-environment-variable", required=True)
    parser.add_argument("--auth-token-environment-variable", required=True)
    parser.add_argument("--snapshot-signing-key-environment-variable", required=True)
    parser.add_argument("--credential-binding-key-environment-variable", required=True)
    parser.add_argument("--expected-webhook-environment-variable", required=True)
    parser.add_argument("--expected-callback-environment-variable", required=True)
    parser.add_argument("--destination-project-id", required=True)
    parser.add_argument("--destination-deployment-id", required=True)
    parser.add_argument("--destination-deployment-revision", required=True)
    parser.add_argument("--database-identity-sha256", required=True)
    parser.add_argument("--challenge-nonce", required=True)
    parser.add_argument("--cutover-window-evidence-id", required=True)
    parser.add_argument("--evidence-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        source_env = os.environ
        account_env = environment_name(
            args.account_sid_environment_variable,
            reason_code="account_sid_environment_variable_invalid",
        )
        credential_env = environment_name(
            args.auth_token_environment_variable,
            reason_code="auth_token_environment_variable_invalid",
        )
        signing_env = environment_name(
            args.snapshot_signing_key_environment_variable,
            reason_code="snapshot_signing_key_environment_variable_invalid",
        )
        binding_env = environment_name(
            args.credential_binding_key_environment_variable,
            reason_code="credential_binding_key_environment_variable_invalid",
        )
        require(
            len({account_env, credential_env, signing_env, binding_env}) == 4,
            "provider_collector_key_environment_variables_must_differ",
        )
        account = required_environment_value(
            source_env,
            account_env,
            missing_reason="twilio_account_sid_missing",
        )
        credential = required_environment_value(
            source_env,
            credential_env,
            missing_reason="twilio_auth_token_missing",
        )
        signing_key = required_environment_value(
            source_env,
            signing_env,
            missing_reason="snapshot_signing_key_missing",
        )
        binding_key = required_environment_value(
            source_env,
            binding_env,
            missing_reason="credential_binding_key_missing",
        )
        require(
            not hmac.compare_digest(signing_key, credential)
            and not hmac.compare_digest(binding_key, credential)
            and not hmac.compare_digest(signing_key, binding_key),
            "provider_collector_secret_values_must_differ",
        )
        client = TwilioV2GetOnlyClient(
            account_sid_value=account,
            auth_token=credential,
        )
        binding = credential_binding(
            binding_key=binding_key,
            account_sid_value=account,
            credential_value=credential,
        )
        document = collect_snapshot_document(
            client,
            tenant_id=args.tenant_id,
            credential_environment_variable=credential_env,
            signing_key_environment_variable=signing_env,
            credential_binding_key_environment_variable=binding_env,
            credential_binding_hmac_sha256=binding,
            expected_webhook_url=required_environment_value(
                source_env,
                args.expected_webhook_environment_variable,
                missing_reason="expected_webhook_url_missing",
            ),
            expected_status_callback_url=required_environment_value(
                source_env,
                args.expected_callback_environment_variable,
                missing_reason="expected_status_callback_url_missing",
            ),
            destination_project_id=args.destination_project_id,
            destination_deployment_id=args.destination_deployment_id,
            destination_deployment_revision=args.destination_deployment_revision,
            database_identity_sha256=args.database_identity_sha256,
            challenge_nonce=args.challenge_nonce,
            cutover_window_evidence_id=args.cutover_window_evidence_id,
            evidence_id_value=args.evidence_id,
        )
        print(json.dumps(signed_envelope(document, signing_key=signing_key), sort_keys=True))
        return 0
    except CutoverEvidenceError as exc:
        print(
            json.dumps(
                {
                    "contract_version": CONTRACT_VERSION,
                    "status": "blocked",
                    "read_only": True,
                    "mutations_performed": False,
                    "messages_sent": False,
                    "reason_code": exc.reason_code,
                },
                sort_keys=True,
            )
        )
        return 2
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        print(
            json.dumps(
                {
                    "contract_version": CONTRACT_VERSION,
                    "status": "blocked",
                    "read_only": True,
                    "mutations_performed": False,
                    "messages_sent": False,
                    "reason_code": "twilio_provider_collector_unexpected_error",
                    "error_type": type(exc).__name__,
                },
                sort_keys=True,
            )
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())

