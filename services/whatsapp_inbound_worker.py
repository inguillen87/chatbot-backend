"""DB-backed WhatsApp inbound replay and ordered Twilio outbox dispatch.

The public webhook validates the provider request and persists an allowlisted
turn.  This module claims that durable row, re-resolves its tenant/provider
scope, invokes the existing response pipeline inside a local Flask request
context, and captures every Twilio send into the transactional outbox.

No HTTP request is made back into this application and no Twilio signature is
fabricated during replay.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import logging
import os
import re
import signal
import threading
from types import SimpleNamespace
from typing import Any, Iterator, Mapping, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from flask import current_app, g, has_app_context
from sqlalchemy.orm import joinedload
from twilio.rest import Client
from werkzeug.exceptions import HTTPException

from celery_utils import celery_app
from extensions import db
from models import ProviderSender, TenantProfile, WhatsAppOutboundAttempt
from services.llm_provider_network_policy import require_provider_network
from services.provider_platform import is_sender_ready_status
from services.twilio_tech_provider import (
    TwilioRuntimeCredentials,
    resolve_twilio_runtime_credentials,
)
from services.whatsapp_inbound_durability import (
    WhatsAppInboundDurabilityConfigurationError,
    resolve_whatsapp_inbound_durability_policy,
    resolve_whatsapp_inbound_queue_tenants,
)
from services.whatsapp_inbound_turns import (
    MAX_PAYLOAD_SCRUB_BATCH_SIZE,
    PAYLOAD_SCRUB_RUN_CONTRACT_VERSION,
    InboundTurnClaim,
    OutboundAttemptClaim,
    accept_whatsapp_outbound_attempt,
    claim_next_whatsapp_inbound_turn,
    claim_next_whatsapp_outbound_attempt,
    complete_whatsapp_inbound_turn,
    dead_whatsapp_outbound_attempt,
    dead_whatsapp_inbound_turn,
    normalize_whatsapp_outbound_payload,
    renew_whatsapp_inbound_turn_lease,
    retry_whatsapp_outbound_attempt,
    retry_whatsapp_inbound_turn,
    scrub_expired_whatsapp_inbound_payloads,
    summarize_whatsapp_turn_health,
    uncertain_whatsapp_outbound_attempt,
)


logger = logging.getLogger(__name__)

INBOUND_TASK_NAME = "whatsapp.process_inbound_stream"
INBOUND_SWEEP_TASK_NAME = "whatsapp.sweep_inbound_turns"
OUTBOUND_TASK_NAME = "whatsapp.dispatch_outbound_attempts"
PAYLOAD_SCRUB_TASK_NAME = "whatsapp.scrub_expired_inbound_payloads"
_DEFAULT_STATUS_CALLBACK_CONNECTION_OVERRIDES = "rc=2&rp=5xx,ct,rt"
_PAYLOAD_SCRUB_BATCH_CONTRACT_VERSION = "whatsapp.inbound_payload_scrub_batch.v1"
_STANDBY_CONTRACT_VERSION = "whatsapp.durable_worker_standby.v1"
_ROUND_ROBIN_EXTENSION_KEY = "chatboc.whatsapp_durable_worker.round_robin"
_ROUND_ROBIN_LOCK = threading.Lock()

INTERVIEW_CONSENT_PROVIDER_RECEIPT_CONTRACT_VERSION = (
    "interview.consent_provider_receipt.v3"
)
_INTERVIEW_CONSENT_ACTION = re.compile(
    r"interview_consent_v3:([A-Za-z0-9_-]{43}):([0-9a-f]{64}):grant_consent\Z"
)

_OUTBOUND_PAYLOAD_FIELDS = frozenset(
    {
        "_chatboc_policy_metadata",
        "body",
        "content_sid",
        "content_variables",
        "from_",
        "media_url",
        "messaging_service_sid",
        "persistent_action",
        "status_callback",
        "to",
    }
)


class WhatsAppWorkerScopeError(RuntimeError):
    """A durable row no longer matches its tenant/provider ownership scope."""


def _rotate_tenant_ids(tenant_ids: tuple[int, ...]) -> tuple[int, ...]:
    """Rotate canaries once per process call without cross-thread races."""

    if len(tenant_ids) < 2:
        return tenant_ids
    with _ROUND_ROBIN_LOCK:
        cursors = current_app.extensions.setdefault(_ROUND_ROBIN_EXTENSION_KEY, {})
        start = int(cursors.get(tenant_ids, 0)) % len(tenant_ids)
        cursors[tenant_ids] = (start + 1) % len(tenant_ids)
    return tenant_ids[start:] + tenant_ids[:start]


def _worker_tenant_ids(tenant_id: Optional[int]) -> tuple[int, ...]:
    """Return only configured canaries; explicit non-canaries are rejected."""

    if not has_app_context():
        raise WhatsAppInboundDurabilityConfigurationError(
            "whatsapp_inbound_worker_app_context_required"
        )
    canaries = resolve_whatsapp_inbound_queue_tenants(current_app.config)
    if tenant_id is None:
        return _rotate_tenant_ids(tuple(sorted(canaries)))
    if isinstance(tenant_id, bool):
        raise WhatsAppInboundDurabilityConfigurationError(
            "whatsapp_inbound_worker_tenant_invalid"
        )
    try:
        requested_tenant = int(tenant_id)
    except (TypeError, ValueError, OverflowError) as exc:
        raise WhatsAppInboundDurabilityConfigurationError(
            "whatsapp_inbound_worker_tenant_invalid"
        ) from exc
    if requested_tenant <= 0:
        raise WhatsAppInboundDurabilityConfigurationError(
            "whatsapp_inbound_worker_tenant_invalid"
        )
    if requested_tenant not in canaries:
        raise WhatsAppInboundDurabilityConfigurationError(
            "whatsapp_inbound_worker_tenant_not_canary"
        )
    return (requested_tenant,)


def _extract_interview_consent_provider_receipt(
    payload: Mapping[str, Any],
) -> Optional[dict[str, Any]]:
    """Return an interview consent receipt from an exact provider control.

    ``payload`` is the allowlisted payload already persisted after provider
    authentication.  Free-form ``Body`` and visible button labels are
    deliberately ignored: only the opaque action value delivered in
    ``ButtonPayload`` or ``ListId`` can grant consent.  Conflicting interactive
    values fail closed.
    """

    interactive_values: list[str] = []
    for field in ("ButtonPayload", "ListId"):
        value = payload.get(field)
        if value in (None, ""):
            continue
        if not isinstance(value, str):
            return None
        interactive_values.append(value)

    if not interactive_values or len(set(interactive_values)) != 1:
        return None

    match = _INTERVIEW_CONSENT_ACTION.fullmatch(interactive_values[0])
    if match is None:
        return None

    nonce = match.group(1)
    return {
        "contract_version": INTERVIEW_CONSENT_PROVIDER_RECEIPT_CONTRACT_VERSION,
        "challenge_nonce_sha256": hashlib.sha256(nonce.encode("ascii")).hexdigest(),
        "consent_text_sha256": match.group(2),
        "action": "grant_consent",
        "granted": True,
    }


@dataclass(frozen=True)
class InboundProcessingResult:
    status: str
    turn_id: Optional[str]
    tenant_id: Optional[int]
    outbound_count: int = 0
    response_status: Optional[int] = None
    error_code: Optional[str] = None


@dataclass(frozen=True)
class OutboundDispatchResult:
    status: str
    attempt_id: Optional[str]
    tenant_id: Optional[int]
    provider_message_sid: Optional[str] = None
    error_code: Optional[str] = None


class WhatsAppOutboundCollector:
    """Collect legacy Twilio calls as ordered durable outbox specifications."""

    def __init__(self, *, turn_id: str) -> None:
        self.turn_id = str(turn_id)
        self._delay_stack: list[int] = [0]
        self._outbound: list[dict[str, Any]] = []

    @property
    def outbound(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._outbound)

    @contextmanager
    def delay(self, seconds: Any) -> Iterator[None]:
        if isinstance(seconds, bool):
            normalized = 0
        else:
            try:
                normalized = max(0, int(seconds or 0))
            except (TypeError, ValueError, OverflowError):
                normalized = 0
        self._delay_stack.append(normalized)
        try:
            yield
        finally:
            self._delay_stack.pop()

    def capture(self, params: Mapping[str, Any]) -> SimpleNamespace:
        raw = dict(params or {})
        if "from" in raw and "from_" not in raw:
            raw["from_"] = raw.pop("from")
        payload = {
            key: value
            for key, value in raw.items()
            if key in _OUTBOUND_PAYLOAD_FIELDS and value is not None
        }
        normalized = normalize_whatsapp_outbound_payload(payload)
        sequence_no = len(self._outbound) + 1
        message_kind = _infer_outbound_kind(normalized)
        self._outbound.append(
            {
                "message_kind": message_kind,
                "payload": normalized,
                "delay_seconds": self._delay_stack[-1],
                "idempotency_key": (
                    f"whatsapp-turn:{self.turn_id}:captured:{sequence_no}"
                ),
            }
        )
        synthetic_sid = "SMQ" + hashlib.sha256(
            f"{self.turn_id}:{sequence_no}".encode("utf-8")
        ).hexdigest()[:29]
        return SimpleNamespace(sid=synthetic_sid, status="queued")


class _InboundLeaseHeartbeat:
    """Keep a long STT/vision/LLM turn fenced while it is executing."""

    def __init__(self, *, app: Any, claim: InboundTurnClaim, lease_seconds: int) -> None:
        self.app = app
        self.claim = claim
        self.lease_seconds = max(30, min(int(lease_seconds), 3600))
        self.interval = max(5.0, min(self.lease_seconds / 3.0, 60.0))
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"whatsapp-lease-{claim.turn_id[:8]}",
            daemon=True,
        )

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=min(self.interval + 1.0, 5.0))

    def renew_now(self) -> bool:
        if self.lost:
            return False
        renewed = renew_whatsapp_inbound_turn_lease(
            self.claim.turn_id,
            self.claim.lease_token,
            lease_seconds=self.lease_seconds,
        )
        if renewed is None:
            self._lost.set()
            return False
        return True

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                with self.app.app_context():
                    if not self.renew_now():
                        return
                    db.session.remove()
            except Exception as exc:
                # Do not claim ownership after a failed renewal. The main
                # worker verifies the fence once more before completion.
                self._lost.set()
                logger.error(
                    "[WHATSAPP_WORKER] Inbound lease heartbeat failed "
                    "turn_id=%s error_type=%s",
                    self.claim.turn_id,
                    type(exc).__name__,
                )
                return


def _infer_outbound_kind(payload: Mapping[str, Any]) -> str:
    if payload.get("content_sid"):
        return "template"
    if payload.get("persistent_action"):
        return "interactive"
    media_urls = payload.get("media_url") or []
    if isinstance(media_urls, str):
        media_urls = [media_urls]
    if media_urls:
        first = str(media_urls[0] or "").lower().split("?", 1)[0]
        if first.endswith((".aac", ".m4a", ".mp3", ".ogg", ".opus", ".wav")):
            return "audio"
        return "media"
    return "text"


def _normalize_address(value: Any) -> str:
    rendered = str(value or "").strip().lower()
    if rendered.startswith("whatsapp:"):
        rendered = rendered.removeprefix("whatsapp:").strip()
    return rendered


def _load_claim_scope(
    claim: InboundTurnClaim | OutboundAttemptClaim,
) -> tuple[TenantProfile, Optional[ProviderSender], TwilioRuntimeCredentials]:
    tenant = db.session.get(TenantProfile, int(claim.tenant_id))
    if tenant is None:
        raise WhatsAppWorkerScopeError("tenant_scope_missing")

    sender: Optional[ProviderSender] = None
    if claim.provider_sender_id is not None:
        sender = (
            ProviderSender.query.options(
                joinedload(ProviderSender.tenant),
                joinedload(ProviderSender.provider_connection),
            )
            .filter(ProviderSender.id == int(claim.provider_sender_id))
            .first()
        )
        if sender is None:
            raise WhatsAppWorkerScopeError("provider_sender_missing")
        if int(sender.tenant_id or 0) != int(claim.tenant_id):
            raise WhatsAppWorkerScopeError("provider_sender_tenant_mismatch")
        if str(sender.channel or "").strip().lower() != "whatsapp":
            raise WhatsAppWorkerScopeError("provider_sender_channel_mismatch")
        if not is_sender_ready_status(sender.status):
            raise WhatsAppWorkerScopeError("provider_sender_not_ready")
        if sender.provider_connection_id != claim.provider_connection_id:
            raise WhatsAppWorkerScopeError("provider_connection_scope_mismatch")
    elif claim.provider_connection_id is not None:
        raise WhatsAppWorkerScopeError("provider_connection_without_sender")

    credentials = resolve_twilio_runtime_credentials(
        tenant=tenant,
        provider_connection=(
            getattr(sender, "provider_connection", None) if sender else None
        ),
        app_config=current_app.config,
    )
    if not credentials.ready:
        raise WhatsAppWorkerScopeError("twilio_credentials_unavailable")
    return tenant, sender, credentials


def _validate_inbound_claim_scope(
    claim: InboundTurnClaim,
    *,
    sender: Optional[ProviderSender],
    credentials: TwilioRuntimeCredentials,
) -> None:
    payload = claim.payload
    request_account_sid = str(payload.get("AccountSid") or "").strip()
    if request_account_sid and request_account_sid != credentials.account_sid:
        raise WhatsAppWorkerScopeError("twilio_account_scope_mismatch")

    if sender is None:
        return
    expected_destination = _normalize_address(
        sender.phone_number or sender.sender_id
    )
    actual_destination = _normalize_address(payload.get("To"))
    if expected_destination and actual_destination != expected_destination:
        raise WhatsAppWorkerScopeError("twilio_destination_scope_mismatch")
    service_sid = str(
        payload.get("MessagingServiceSid") or payload.get("ServiceSid") or ""
    ).strip()
    if (
        service_sid
        and sender.messaging_service_sid
        and service_sid != sender.messaging_service_sid
    ):
        raise WhatsAppWorkerScopeError("twilio_service_scope_mismatch")


def _internal_base_url() -> str:
    for key in (
        "PUBLIC_API_BASE_URL",
        "BACKEND_URL",
        "API_BASE_URL",
        "APP_BASE_URL",
        "BASE_URL",
    ):
        candidate = str(current_app.config.get(key) or "").strip().rstrip("/")
        if candidate.startswith(("http://", "https://")):
            return candidate
    return "http://localhost"


def _response_status_code(response: Any) -> int:
    if isinstance(response, tuple) and len(response) >= 2:
        try:
            return int(response[1])
        except (TypeError, ValueError, OverflowError):
            return 500
    status_code = getattr(response, "status_code", None)
    if status_code is not None:
        try:
            return int(status_code)
        except (TypeError, ValueError, OverflowError):
            return 500
    return 200


def process_whatsapp_inbound_claim(
    claim: InboundTurnClaim,
) -> InboundProcessingResult:
    """Replay one claimed turn locally and stage all outbound messages."""

    lease_seconds = int(
        current_app.config.get("WHATSAPP_INBOUND_LEASE_SECONDS", 180) or 180
    )
    renewed = renew_whatsapp_inbound_turn_lease(
        claim.turn_id,
        claim.lease_token,
        lease_seconds=lease_seconds,
    )
    if renewed is None:
        return InboundProcessingResult(
            status="lost_lease",
            turn_id=claim.turn_id,
            tenant_id=claim.tenant_id,
            error_code="inbound_lease_not_owned",
        )

    collector = WhatsAppOutboundCollector(turn_id=claim.turn_id)
    app = current_app._get_current_object()
    heartbeat = _InboundLeaseHeartbeat(
        app=app,
        claim=claim,
        lease_seconds=lease_seconds,
    )
    heartbeat.start()
    try:
        _, sender, credentials = _load_claim_scope(claim)
        _validate_inbound_claim_scope(
            claim,
            sender=sender,
            credentials=credentials,
        )
        interview_consent_receipt = (
            _extract_interview_consent_provider_receipt(claim.payload)
            if claim.message_kind == "interactive"
            else None
        )

        # Local function invocation only. The route reads claim.payload from g,
        # skips signature/ingress/legacy JSON dedupe, and rechecks DB scope.
        from routes import whatsapp_webhook as webhook_module

        with app.test_request_context(
            "/webhook/whatsapp",
            method="POST",
            base_url=_internal_base_url(),
        ):
            g.whatsapp_inbound_turn_claim = claim
            g.whatsapp_outbound_collector = collector
            response = webhook_module.whatsapp_webhook()

        heartbeat.stop()
        if not heartbeat.renew_now():
            return InboundProcessingResult(
                status="lost_lease",
                turn_id=claim.turn_id,
                tenant_id=claim.tenant_id,
                outbound_count=len(collector.outbound),
                error_code="inbound_lease_heartbeat_lost",
            )

        status_code = _response_status_code(response)
        if status_code >= 500:
            raise RuntimeError(f"webhook_replay_http_{status_code}")
        if status_code >= 400:
            raise WhatsAppWorkerScopeError(
                f"webhook_replay_rejected_{status_code}"
            )

        worker_result: dict[str, Any] = {
            "contract_version": "whatsapp.worker_result.v1",
            "response_status": status_code,
            "outbound_count": len(collector.outbound),
        }
        if interview_consent_receipt is not None:
            worker_result["interview_consent_receipt"] = (
                interview_consent_receipt
            )

        completion = complete_whatsapp_inbound_turn(
            claim.turn_id,
            claim.lease_token,
            result=worker_result,
            outbound=collector.outbound,
        )
        if not completion.completed:
            return InboundProcessingResult(
                status="lost_lease",
                turn_id=claim.turn_id,
                tenant_id=claim.tenant_id,
                outbound_count=len(collector.outbound),
                response_status=status_code,
                error_code="completion_fence_rejected",
            )
        return InboundProcessingResult(
            status="completed",
            turn_id=claim.turn_id,
            tenant_id=claim.tenant_id,
            outbound_count=len(completion.outbound_attempt_ids),
            response_status=status_code,
        )
    except WhatsAppWorkerScopeError as exc:
        heartbeat.stop()
        error_code = str(exc)
        state = dead_whatsapp_inbound_turn(
            claim.turn_id,
            claim.lease_token,
            error_code,
        )
        return InboundProcessingResult(
            status=state or "lost_lease",
            turn_id=claim.turn_id,
            tenant_id=claim.tenant_id,
            outbound_count=len(collector.outbound),
            error_code=error_code,
        )
    except HTTPException as exc:
        heartbeat.stop()
        status_code = int(getattr(exc, "code", 500) or 500)
        error_code = f"webhook_replay_http_{status_code}"
        if status_code >= 500:
            db.session.rollback()
            state = retry_whatsapp_inbound_turn(
                claim.turn_id,
                claim.lease_token,
                error_code,
            )
        else:
            state = dead_whatsapp_inbound_turn(
                claim.turn_id,
                claim.lease_token,
                error_code,
            )
        return InboundProcessingResult(
            status=state or "lost_lease",
            turn_id=claim.turn_id,
            tenant_id=claim.tenant_id,
            outbound_count=len(collector.outbound),
            response_status=status_code,
            error_code=error_code,
        )
    except Exception as exc:
        heartbeat.stop()
        db.session.rollback()
        state = retry_whatsapp_inbound_turn(
            claim.turn_id,
            claim.lease_token,
            exc,
        )
        logger.warning(
            "[WHATSAPP_WORKER] Inbound replay failed turn_id=%s "
            "tenant_id=%s error_type=%s state=%s",
            claim.turn_id,
            claim.tenant_id,
            type(exc).__name__,
            state,
        )
        return InboundProcessingResult(
            status=state or "lost_lease",
            turn_id=claim.turn_id,
            tenant_id=claim.tenant_id,
            outbound_count=len(collector.outbound),
            error_code=type(exc).__name__,
        )


def process_whatsapp_inbound_stream(
    *,
    tenant_id: Optional[int] = None,
    stream_key: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict[str, Any]:
    """Drain FIFO turns only for tenants in the explicit queue allowlist."""

    tenant_ids = _worker_tenant_ids(tenant_id)
    if stream_key is not None and tenant_id is None:
        raise WhatsAppInboundDurabilityConfigurationError(
            "whatsapp_inbound_worker_stream_requires_tenant"
        )
    configured_limit = int(
        current_app.config.get("WHATSAPP_INBOUND_WORKER_BATCH_SIZE", 8) or 8
    )
    bounded_limit = max(1, min(int(limit or configured_limit), 100))
    lease_seconds = int(
        current_app.config.get("WHATSAPP_INBOUND_LEASE_SECONDS", 180) or 180
    )
    results: list[dict[str, Any]] = []
    remaining = bounded_limit
    for tenant_index, current_tenant_id in enumerate(tenant_ids):
        if remaining <= 0:
            break
        tenants_remaining = len(tenant_ids) - tenant_index
        tenant_limit = max(
            1,
            (remaining + tenants_remaining - 1) // tenants_remaining,
        )
        for _ in range(tenant_limit):
            claim = claim_next_whatsapp_inbound_turn(
                tenant_id=current_tenant_id,
                stream_key=stream_key,
                lease_seconds=lease_seconds,
            )
            if claim is None:
                break
            result = process_whatsapp_inbound_claim(claim)
            results.append(asdict(result))
            remaining -= 1
            if result.status != "completed":
                break
    return {
        "contract_version": "whatsapp.inbound_worker.v1",
        "processed": len(results),
        "completed": sum(item["status"] == "completed" for item in results),
        "results": results,
    }


def _clean_callback_url(value: Any) -> Optional[str]:
    cleaned = str(value or "").strip().rstrip("/")
    if not cleaned:
        return None
    parsed = urlsplit(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1"}:
        parsed = parsed._replace(scheme="https")
    return urlunsplit(parsed)


def _status_callback_for_attempt(
    claim: OutboundAttemptClaim,
    *,
    sender: Optional[ProviderSender],
) -> str:
    callback = _clean_callback_url(claim.payload.get("status_callback"))
    callback = callback or _clean_callback_url(
        getattr(sender, "status_callback_url", None) if sender else None
    )
    if callback is None:
        configured = (
            current_app.config.get("TWILIO_WHATSAPP_STATUS_CALLBACK_URL")
            or current_app.config.get("WHATSAPP_STATUS_CALLBACK_URL")
        )
        callback = _clean_callback_url(configured)
    if callback is None:
        for key in (
            "PUBLIC_API_BASE_URL",
            "BACKEND_URL",
            "API_BASE_URL",
            "APP_BASE_URL",
            "BASE_URL",
            "RENDER_EXTERNAL_URL",
        ):
            base = _clean_callback_url(current_app.config.get(key))
            if base:
                callback = f"{base}/twilio/whatsapp/status"
                break
    if callback is None:
        raise WhatsAppWorkerScopeError("status_callback_unavailable")

    parsed = urlsplit(callback)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key != "outbound_attempt_id"
    ]
    query.append(("outbound_attempt_id", claim.attempt_id))
    # Twilio's default retry policy covers connection failures only.  Opt the
    # durable reconciliation callback into bounded 5xx/read-timeout retries.
    # URL fragments are connection overrides and are not sent to our server or
    # included in signature computation.
    fragment = parsed.fragment or _DEFAULT_STATUS_CALLBACK_CONNECTION_OVERRIDES
    return urlunsplit(
        parsed._replace(
            query=urlencode(query),
            fragment=fragment,
        )
    )


def _validate_outbound_sender(
    claim: OutboundAttemptClaim,
    sender: Optional[ProviderSender],
) -> None:
    if sender is None:
        return
    configured_from = _normalize_address(
        sender.phone_number or sender.sender_id
    )
    payload_from = _normalize_address(claim.payload.get("from_"))
    if configured_from and payload_from != configured_from:
        raise WhatsAppWorkerScopeError("outbound_sender_scope_mismatch")


def dispatch_next_whatsapp_outbound_attempt(
    *,
    tenant_id: Optional[int] = None,
) -> OutboundDispatchResult:
    """Send one claimed outbox row; ambiguous provider calls never auto-retry."""

    tenant_ids = _worker_tenant_ids(tenant_id)
    claim = None
    for current_tenant_id in tenant_ids:
        claim = claim_next_whatsapp_outbound_attempt(
            tenant_id=current_tenant_id,
            lease_seconds=int(
                current_app.config.get("WHATSAPP_INBOUND_LEASE_SECONDS", 180) or 180
            ),
        )
        if claim is not None:
            break
    if claim is None:
        return OutboundDispatchResult(
            status="idle",
            attempt_id=None,
            tenant_id=tenant_id,
        )

    provider_call_started = False

    def mark_provider_call_started() -> None:
        nonlocal provider_call_started
        provider_call_started = True

    try:
        _, sender, credentials = _load_claim_scope(claim)
        _validate_outbound_sender(claim, sender)
        params = dict(claim.payload)
        params["status_callback"] = _status_callback_for_attempt(
            claim,
            sender=sender,
        )
        params["_chatboc_provider_call_hook"] = mark_provider_call_started

        # Use a nested app context so an inbound collector from an unrelated
        # caller can never intercept the real provider dispatch.
        app = current_app._get_current_object()
        with app.app_context():
            from routes.whatsapp_webhook import _send_twilio_message

            require_provider_network("twilio", app)
            client = Client(credentials.account_sid, credentials.auth_token)
            provider_message = _send_twilio_message(client, **params)
        provider_sid = str(getattr(provider_message, "sid", None) or "").strip()
        if not provider_sid:
            raise RuntimeError("twilio_provider_sid_missing")
        accepted = accept_whatsapp_outbound_attempt(
            claim.attempt_id,
            claim.lease_token,
            provider_sid,
            provider_status=getattr(provider_message, "status", None) or "queued",
        )
        if not accepted:
            return OutboundDispatchResult(
                status="lost_lease",
                attempt_id=claim.attempt_id,
                tenant_id=claim.tenant_id,
                provider_message_sid=provider_sid,
                error_code="outbound_accept_fence_rejected",
            )
        return OutboundDispatchResult(
            status="accepted",
            attempt_id=claim.attempt_id,
            tenant_id=claim.tenant_id,
            provider_message_sid=provider_sid,
        )
    except Exception as exc:
        db.session.rollback()
        if provider_call_started:
            uncertain_whatsapp_outbound_attempt(
                claim.attempt_id,
                claim.lease_token,
                exc,
            )
            state = WhatsAppOutboundAttempt.STATUS_SEND_UNCERTAIN
        elif bool(getattr(exc, "whatsapp_outbound_permanent", False)):
            state = dead_whatsapp_outbound_attempt(
                claim.attempt_id,
                claim.lease_token,
                exc,
            )
        else:
            state = retry_whatsapp_outbound_attempt(
                claim.attempt_id,
                claim.lease_token,
                exc,
            )
        logger.warning(
            "[WHATSAPP_WORKER] Outbound dispatch failed attempt_id=%s "
            "tenant_id=%s provider_call_started=%s state=%s error_type=%s",
            claim.attempt_id,
            claim.tenant_id,
            provider_call_started,
            state,
            type(exc).__name__,
        )
        return OutboundDispatchResult(
            status=state or "lost_lease",
            attempt_id=claim.attempt_id,
            tenant_id=claim.tenant_id,
            error_code=type(exc).__name__,
        )


def dispatch_whatsapp_outbound_attempts(
    *,
    tenant_id: Optional[int] = None,
    limit: int = 20,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for _ in range(max(1, min(int(limit or 1), 100))):
        result = dispatch_next_whatsapp_outbound_attempt(tenant_id=tenant_id)
        if result.status == "idle":
            break
        results.append(asdict(result))
        if result.status != "accepted":
            break
    return {
        "contract_version": "whatsapp.outbound_worker.v1",
        "processed": len(results),
        "accepted": sum(item["status"] == "accepted" for item in results),
        "results": results,
    }


def enqueue_whatsapp_inbound_stream(*, tenant_id: int, stream_key: str) -> bool:
    """Best-effort broker wakeup; the committed DB queue remains authoritative."""

    if not has_app_context():
        return False
    policy = resolve_whatsapp_inbound_durability_policy(
        current_app.config,
        tenant_id=tenant_id,
    )
    if not policy.queue_enabled:
        return False
    if (
        current_app.testing
        or not bool(
            current_app.config.get(
                "WHATSAPP_INBOUND_CELERY_WAKEUP_ENABLED",
                False,
            )
        )
    ):
        return False
    process_whatsapp_inbound_stream_task.apply_async(
        args=[int(tenant_id), str(stream_key)],
        retry=False,
        ignore_result=True,
    )
    return True


@celery_app.task(name=INBOUND_TASK_NAME, acks_late=True, ignore_result=True)
def process_whatsapp_inbound_stream_task(tenant_id: int, stream_key: str) -> dict[str, Any]:
    return process_whatsapp_inbound_stream(
        tenant_id=int(tenant_id),
        stream_key=str(stream_key),
    )


@celery_app.task(name=INBOUND_SWEEP_TASK_NAME, acks_late=True, ignore_result=True)
def sweep_whatsapp_inbound_turns_task(limit: int = 20) -> dict[str, Any]:
    return process_whatsapp_inbound_stream(limit=limit)


@celery_app.task(name=OUTBOUND_TASK_NAME, acks_late=True, ignore_result=True)
def dispatch_whatsapp_outbound_attempts_task(limit: int = 20) -> dict[str, Any]:
    return dispatch_whatsapp_outbound_attempts(limit=limit)


def _strict_positive_tenant_ids(raw_value: Any) -> tuple[int, ...]:
    tokens = [token.strip() for token in str(raw_value or "").split(",")]
    tenant_ids: set[int] = set()
    for token in tokens:
        if not token:
            continue
        try:
            tenant_id = int(token)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError("whatsapp_inbound_tenant_ids_invalid") from exc
        if tenant_id <= 0:
            raise RuntimeError("whatsapp_inbound_tenant_ids_invalid")
        tenant_ids.add(tenant_id)
    return tuple(sorted(tenant_ids))


def _blocked_payload_scrub_report(status: str) -> dict[str, Any]:
    return {
        "contract_version": _PAYLOAD_SCRUB_BATCH_CONTRACT_VERSION,
        "status": status,
        "tenant_count": 0,
        "selected": 0,
        "scrubbed": 0,
        "completed_scrubbed": 0,
        "dead_scrubbed": 0,
    }


def run_whatsapp_inbound_payload_scrub(*, limit: Optional[int] = None) -> dict[str, Any]:
    """Run one bounded, tenant-scoped retention batch after every safety gate."""

    scrub_enabled = current_app.config.get(
        "WHATSAPP_INBOUND_PAYLOAD_SCRUB_ENABLED",
        False,
    )
    if not isinstance(scrub_enabled, bool):
        raise RuntimeError("whatsapp_inbound_payload_scrub_enabled_invalid")
    configured_hold = current_app.config.get(
        "WHATSAPP_INBOUND_PAYLOAD_LEGAL_HOLD",
        True,
    )
    if not isinstance(configured_hold, bool):
        raise RuntimeError("whatsapp_inbound_payload_legal_hold_invalid")

    # These exits deliberately happen before any query. A scheduled process can
    # therefore exist in the Blueprint without reading or deleting payloads.
    if not scrub_enabled:
        return _blocked_payload_scrub_report("disabled")
    if configured_hold:
        return _blocked_payload_scrub_report("legal_hold")

    tenant_ids = _strict_positive_tenant_ids(
        current_app.config.get("WHATSAPP_INBOUND_PAYLOAD_SCRUB_TENANT_IDS", "")
    )
    if not tenant_ids:
        return _blocked_payload_scrub_report("no_retention_tenants")

    raw_limit = (
        limit
        if limit is not None
        else current_app.config.get(
            "WHATSAPP_INBOUND_PAYLOAD_SCRUB_BATCH_SIZE",
            200,
        )
    )
    try:
        batch_limit = int(raw_limit)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError("whatsapp_inbound_payload_scrub_batch_size_invalid") from exc
    if batch_limit < 1 or batch_limit > MAX_PAYLOAD_SCRUB_BATCH_SIZE:
        raise RuntimeError("whatsapp_inbound_payload_scrub_batch_size_invalid")

    retention_hours = current_app.config.get(
        "WHATSAPP_INBOUND_DEAD_PAYLOAD_RETENTION_HOURS",
        72,
    )
    remaining = batch_limit
    tenant_reports: list[dict[str, Any]] = []
    for tenant_index, tenant_id in enumerate(tenant_ids):
        if remaining <= 0:
            break
        tenants_remaining = len(tenant_ids) - tenant_index
        tenant_limit = max(1, (remaining + tenants_remaining - 1) // tenants_remaining)
        tenant_report = scrub_expired_whatsapp_inbound_payloads(
            dead_retention_hours=retention_hours,
            legal_hold=False,
            limit=tenant_limit,
            tenant_id=tenant_id,
        )
        tenant_reports.append(tenant_report)
        remaining -= int(tenant_report.get("selected") or 0)

    report = {
        "contract_version": _PAYLOAD_SCRUB_BATCH_CONTRACT_VERSION,
        "status": "completed",
        "source_contract_version": PAYLOAD_SCRUB_RUN_CONTRACT_VERSION,
        "tenant_count": len(tenant_reports),
        "selected": sum(int(item.get("selected") or 0) for item in tenant_reports),
        "scrubbed": sum(int(item.get("scrubbed") or 0) for item in tenant_reports),
        "completed_scrubbed": sum(
            int(item.get("completed_scrubbed") or 0) for item in tenant_reports
        ),
        "dead_scrubbed": sum(
            int(item.get("dead_scrubbed") or 0) for item in tenant_reports
        ),
        "batch_limit": batch_limit,
        "batch_remaining": remaining,
    }
    logger.info(
        "[WHATSAPP_RETENTION] status=%s tenants=%s selected=%s scrubbed=%s",
        report["status"],
        report["tenant_count"],
        report["selected"],
        report["scrubbed"],
    )
    return report


@celery_app.task(name=PAYLOAD_SCRUB_TASK_NAME, acks_late=True)
def scrub_expired_whatsapp_inbound_payloads_task(
    limit: Optional[int] = None,
) -> dict[str, Any]:
    return run_whatsapp_inbound_payload_scrub(limit=limit)


def run_whatsapp_durable_worker(
    app: Any,
    *,
    once: bool = False,
    stop_event: Optional[threading.Event] = None,
    standby_when_legacy: bool = False,
) -> dict[str, Any]:
    """Run the authoritative DB poller for inbound turns and outbound sends."""

    shutdown = stop_event or threading.Event()
    totals = {
        "cycles": 0,
        "inbound_completed": 0,
        "outbound_accepted": 0,
    }
    with app.app_context():
        mode = str(
            current_app.config.get("WHATSAPP_INBOUND_DURABILITY_MODE", "legacy")
            or "legacy"
        ).strip().lower()
        if mode == "legacy" and standby_when_legacy:
            logger.warning(
                "[WHATSAPP_WORKER] Standby: durable queue remains disabled."
            )
            if not once and not shutdown.is_set():
                # One blocking wait: no polling, DB access, queue access or
                # provider construction while the web service remains legacy.
                shutdown.wait()
            return {
                "contract_version": _STANDBY_CONTRACT_VERSION,
                "status": "standby",
                "mode": "legacy",
                **totals,
            }
        if mode != "queue":
            raise RuntimeError("whatsapp_durable_worker_requires_queue_mode")
        # Validate the full queue contract and explicit worker scope before the
        # first database query. An empty or malformed allowlist must not turn a
        # global poller into a cross-tenant claimant.
        resolve_whatsapp_inbound_queue_tenants(current_app.config)

        # Fail at startup when the migration/table contract is missing instead
        # of running a process that silently acknowledges no work.
        summarize_whatsapp_turn_health()
        poll_seconds = max(
            0.05,
            min(
                float(
                    current_app.config.get(
                        "WHATSAPP_INBOUND_WORKER_POLL_SECONDS",
                        0.5,
                    )
                    or 0.5
                ),
                60.0,
            ),
        )

        while not shutdown.is_set():
            totals["cycles"] += 1
            try:
                inbound = process_whatsapp_inbound_stream()
                outbound = dispatch_whatsapp_outbound_attempts()
                totals["inbound_completed"] += int(inbound.get("completed") or 0)
                totals["outbound_accepted"] += int(outbound.get("accepted") or 0)
                did_work = bool(
                    int(inbound.get("processed") or 0)
                    or int(outbound.get("processed") or 0)
                )
                db.session.remove()
            except Exception as exc:
                db.session.rollback()
                db.session.remove()
                logger.error(
                    "[WHATSAPP_WORKER] Durable poll cycle failed error_type=%s",
                    type(exc).__name__,
                )
                did_work = False

            if once:
                break
            if not did_work:
                shutdown.wait(poll_seconds)

    return {
        "contract_version": "whatsapp.durable_worker_run.v1",
        "status": "completed" if once else "stopped",
        "mode": "queue",
        **totals,
    }


def _strict_env_switch(name: str, *, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise RuntimeError(f"{name.lower()}_invalid")


def _install_shutdown_handlers(shutdown: threading.Event) -> None:
    def _stop(*_args: Any) -> None:
        shutdown.set()

    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signal_name, _stop)
        except (AttributeError, ValueError):
            pass


def main() -> int:
    """Entrypoint for a dedicated Render/background worker process."""

    parser = argparse.ArgumentParser(description="Chatboc durable WhatsApp worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="process one bounded inbound/outbound cycle and exit",
    )
    parser.add_argument(
        "--health",
        action="store_true",
        help="print payload-free queue health and exit",
    )
    parser.add_argument(
        "--scrub-expired-payloads",
        action="store_true",
        help="run one auditable inbound payload-retention batch and exit",
    )
    parser.add_argument(
        "--standby-when-legacy",
        action="store_true",
        help="stay idle without application/DB/provider I/O while mode is legacy",
    )
    args = parser.parse_args()

    default_role = (
        "whatsapp-payload-retention-cron"
        if args.scrub_expired_payloads
        else "whatsapp-durable-worker"
    )
    os.environ.setdefault("CHATBOC_PROCESS_ROLE", default_role)
    os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

    shutdown = threading.Event()
    _install_shutdown_handlers(shutdown)

    durability_mode = str(
        os.getenv("WHATSAPP_INBOUND_DURABILITY_MODE", "legacy") or "legacy"
    ).strip().lower()
    if args.standby_when_legacy and durability_mode == "legacy":
        if not _strict_env_switch(
            "WHATSAPP_DURABLE_WORKER_STANDBY_ENABLED",
            default=False,
        ):
            raise RuntimeError("whatsapp_durable_worker_standby_not_enabled")
        if not args.once and not shutdown.is_set():
            shutdown.wait()
        return 0

    if args.scrub_expired_payloads:
        scrub_enabled = _strict_env_switch(
            "WHATSAPP_INBOUND_PAYLOAD_SCRUB_ENABLED",
            default=False,
        )
        legal_hold = _strict_env_switch(
            "WHATSAPP_INBOUND_PAYLOAD_LEGAL_HOLD",
            default=True,
        )
        scrub_tenant_ids = _strict_positive_tenant_ids(
            os.getenv("WHATSAPP_INBOUND_PAYLOAD_SCRUB_TENANT_IDS", "")
        )
        if (
            not scrub_enabled
            or legal_hold
            or not scrub_tenant_ids
        ):
            status = (
                "disabled"
                if not scrub_enabled
                else "legal_hold"
                if legal_hold
                else "no_retention_tenants"
            )
            print(json.dumps(_blocked_payload_scrub_report(status), sort_keys=True))
            return 0

    from app import create_app
    from config import Config

    app = create_app(Config)
    if args.health:
        with app.app_context():
            print(
                json.dumps(
                    summarize_whatsapp_turn_health(),
                    ensure_ascii=True,
                    sort_keys=True,
                )
            )
        return 0
    if args.scrub_expired_payloads:
        with app.app_context():
            print(
                json.dumps(
                    run_whatsapp_inbound_payload_scrub(),
                    ensure_ascii=True,
                    sort_keys=True,
                )
            )
        return 0

    run_whatsapp_durable_worker(
        app,
        once=args.once,
        stop_event=shutdown,
        standby_when_legacy=args.standby_when_legacy,
    )
    return 0


__all__ = [
    "InboundProcessingResult",
    "OutboundDispatchResult",
    "WhatsAppOutboundCollector",
    "WhatsAppWorkerScopeError",
    "dispatch_next_whatsapp_outbound_attempt",
    "dispatch_whatsapp_outbound_attempts",
    "dispatch_whatsapp_outbound_attempts_task",
    "enqueue_whatsapp_inbound_stream",
    "process_whatsapp_inbound_claim",
    "process_whatsapp_inbound_stream",
    "process_whatsapp_inbound_stream_task",
    "run_whatsapp_durable_worker",
    "run_whatsapp_inbound_payload_scrub",
    "scrub_expired_whatsapp_inbound_payloads_task",
    "sweep_whatsapp_inbound_turns_task",
]


if __name__ == "__main__":
    raise SystemExit(main())
