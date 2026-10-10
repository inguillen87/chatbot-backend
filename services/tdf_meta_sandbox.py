"""Opt-in TDF test-number pilot, isolated from Twilio and production senders.

Uses existing receipt tables. A committed intent precedes the single provider
POST; an interrupted/uncertain attempt is never sent again automatically.
Only published institutional knowledge is exposed. No case, appointment or
health-document mutation is performed by this pilot.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import http.client
import hmac
import json
import re
import ssl
import time
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit

from flask import current_app
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from models import db, MessagingEventLedger, TenantProfile, User, WebhookDelivery
from services import meta_whatsapp_cloud as cloud
from services import meta_whatsapp_webhook as webhook
from services.institutional_assistant import maybe_handle_institutional_question, read_state
from services.tenant_provider_credentials import ProviderCredentialError
from cutover_writer_fence import cutover_writer_fence_enabled
from services.tdf_meta_audio import TdfAudioError, audio_error_message, transcribe_meta_voice_note
from services.tdf_meta_contact_identity import owner_contact_resolver

TENANT_ID, TENANT_SLUG = 46, "tierra-del-fuego"
# Exact Meta test resources observed on 2026-10-10; never the JUNI/shared WABA.
TEST_APP, TEST_WABA, TEST_PHONE = "1719510329487224", "2192778428137676", "1137550632773388"
CONTRACT = "chatboc.tdf_meta_sandbox.v1"
PROVIDER = "meta_tdf_sandbox_v1"
_CONTACT = re.compile(r"^[1-9][0-9]{6,14}$")
_VERSION = re.compile(r"^v[1-9][0-9]{0,2}\.0$")
MAX_GRAPH_BYTES = 64 * 1024


class PilotError(RuntimeError):
    def __init__(self, code, status=503):
        self.code, self.status = code, status
        super().__init__(code)


def settings(config):
    """Strict backend config, no global access-token/legacy transport fallback."""
    if config.get("META_TDF_SANDBOX_ENABLED") is not True:
        raise PilotError("tdf_sandbox_disabled", 404)
    values = {name: config.get("META_TDF_SANDBOX_" + name) for name in
              ("APP_ID", "WABA_ID", "PHONE_NUMBER_ID", "APP_SECRET", "VERIFY_TOKEN", "RECIPIENTS", "GRAPH_VERSION")}
    if (values["APP_ID"], values["WABA_ID"], values["PHONE_NUMBER_ID"]) != (TEST_APP, TEST_WABA, TEST_PHONE):
        raise PilotError("tdf_sandbox_resource_mismatch")
    if (not isinstance(values["APP_SECRET"], str) or not 16 <= len(values["APP_SECRET"]) <= 4096
            or not isinstance(values["VERIFY_TOKEN"], str) or not 16 <= len(values["VERIFY_TOKEN"]) <= 256
            or not isinstance(values["GRAPH_VERSION"], str) or not _VERSION.fullmatch(values["GRAPH_VERSION"])
            or not isinstance(values["RECIPIENTS"], (list, tuple)) or not 1 <= len(values["RECIPIENTS"]) <= 5
            or any(not isinstance(value, str) or not _CONTACT.fullmatch(value) for value in values["RECIPIENTS"])
            or len(set(values["RECIPIENTS"])) != len(values["RECIPIENTS"])):
        raise PilotError("tdf_sandbox_configuration_incomplete")
    return values


def _graph_request(method, url, **kwargs):
    """Fixed HTTPS, bounded body, no retries/proxies/redirects or URL logging.

    http.client keeps debug logging explicitly off. In particular, Meta's
    input_token query is never passed through urllib3's URL debug logger.
    """
    parsed = urlsplit(url)
    if (method not in {"GET", "POST"} or parsed.scheme != "https"
            or parsed.netloc != "graph.facebook.com" or parsed.query or parsed.fragment
            or not parsed.path.startswith("/") or kwargs.get("verify") is not True
            or kwargs.get("allow_redirects") is not False):
        raise PilotError("tdf_meta_transport_invalid")
    query = kwargs.get("params")
    path = parsed.path + ("?" + urlencode(query) if query else "")
    body = json.dumps(kwargs["json"], separators=(",", ":"), allow_nan=False).encode() if "json" in kwargs else None
    connection = http.client.HTTPSConnection("graph.facebook.com", timeout=3.0,
                                             context=ssl.create_default_context())
    connection.set_debuglevel(0)
    deadline = time.monotonic() + 13.0
    try:
        connection.connect()
        connection.sock.settimeout(10.0)
        connection.request(method, path, body=body, headers=kwargs.get("headers", {}))
        response = connection.getresponse()
        chunks, size = [], 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PilotError("tdf_meta_transport_timeout")
            if connection.sock is not None:
                connection.sock.settimeout(min(10.0, remaining))
            chunk = response.read1(8192)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_GRAPH_BYTES:
                raise PilotError("tdf_meta_response_too_large")
            chunks.append(chunk)
        return SimpleNamespace(status_code=response.status, content=b"".join(chunks))
    finally:
        connection.close()


def _json_response(response):
    if type(response.status_code) is not int or not 200 <= response.status_code < 300:
        raise PilotError("tdf_meta_authority_unavailable")
    try:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result
        value = json.loads(response.content.decode("utf-8"), object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except Exception:
        raise PilotError("tdf_meta_authority_invalid") from None


class GraphAuthority:
    """Read token app/scopes, WABA membership and subscribed app for this request."""
    def __init__(self, cfg, now, request_adapter=_graph_request):
        self.cfg, self.now, self.request_adapter, self.cache = cfg, now, request_adapter, {}

    def __call__(self, sender, credential):
        cache_key = (sender, credential.revision, hashlib.sha256(credential.access_token.encode()).digest())
        if cache_key in self.cache:
            return self.cache[cache_key]
        base = "https://graph.facebook.com/" + self.cfg["GRAPH_VERSION"] + "/"
        def get(path, token, params=None):
            try:
                return _json_response(self.request_adapter("GET", base + path,
                    headers={"Authorization": "Bearer " + token}, params=params,
                    timeout=(3.0, 10.0), allow_redirects=False, verify=True))
            except PilotError:
                raise
            except Exception:
                raise PilotError("tdf_meta_authority_unavailable") from None
        # Meta's debug endpoint requires input_token; never log request URLs/errors.
        observed = get("debug_token", TEST_APP + "|" + self.cfg["APP_SECRET"],
                       {"input_token": credential.access_token}).get("data", {})
        if (not isinstance(observed, dict) or observed.get("is_valid") is not True
                or observed.get("app_id") != sender.app_id
                or not isinstance(observed.get("scopes"), list)
                or not {"whatsapp_business_messaging", "whatsapp_business_management"}.issubset(observed["scopes"])
                or type(observed.get("expires_at")) is not int
                or (observed["expires_at"] != 0 and observed["expires_at"] <= self.now)):
            raise PilotError("tdf_meta_token_authority_invalid")
        phones = get(sender.waba_id + "/phone_numbers", credential.access_token,
                     {"fields": "id", "limit": "100"}).get("data")
        if not isinstance(phones, list) or len([row for row in phones
                if isinstance(row, dict) and row.get("id") == sender.phone_number_id]) != 1:
            raise PilotError("tdf_meta_phone_membership_invalid")
        apps = get(sender.waba_id + "/subscribed_apps", credential.access_token).get("data")
        if not isinstance(apps, list) or len([row for row in apps if isinstance(row, dict)
                and isinstance(row.get("whatsapp_business_api_data"), dict)
                and row["whatsapp_business_api_data"].get("id") == sender.app_id]) != 1:
            raise PilotError("tdf_meta_subscription_missing")
        expiry = min(credential.expires_at, self.now + 60)
        if observed["expires_at"]:
            expiry = min(expiry, observed["expires_at"])
        authority = cloud.VerifiedMetaAuthority(sender, credential.revision,
            self.cfg["GRAPH_VERSION"], self.now, expiry, True, True)
        self.cache[cache_key] = authority
        return authority


def binding_loader(cfg, now, authority):
    def load():
        current_now = now() if callable(now) else now
        return cloud.resolve_sender(phone_number_id=cfg["PHONE_NUMBER_ID"],
            app_id=cfg["APP_ID"], waba_id=cfg["WABA_ID"], environment="sandbox", now=current_now,
            app_config=current_app.config, authority_verifier=authority,
            expected_tenant_id=TENANT_ID)
    return load


def _pseudonym(value, cfg):
    return hmac.new(cfg["APP_SECRET"].encode(), value.encode(), hashlib.sha256).hexdigest()


def _contact_key(event, cfg):
    if event.contact_identity is None:
        return _pseudonym(event.contact, cfg)
    # BSUID continuity is namespaced by the authenticated test sender. A phone
    # without its association cannot reconcile a different BSUID's receipt.
    material = [event.sender.tenant_id, event.sender.app_id, event.sender.waba_id,
                event.sender.phone_number_id, event.contact_identity.user_id]
    return _pseudonym(json.dumps(material, separators=(",", ":")), cfg)


def _semantic_digest(event, cfg):
    values = [event.message_id, event.contact, event.content_type,
        event.text, event.selection, event.location, event.status, event.error_codes]
    if event.contact_identity is not None:
        # Retain the authenticated association in the durable digest only; no
        # raw BSUID/phone/name is written to receipt payloads or metadata.
        values.extend([event.contact_identity.user_id, event.contact_identity.parent_user_id])
    if event.content_type == "audio":
        values.extend(["meta_audio_v1", event.media_id, event.media_mime_type, event.media_sha256])
    material = json.dumps(values,
        separators=(",", ":"), ensure_ascii=False)
    return _pseudonym(material, cfg)


def _claim(event, cfg):
    digest = _semantic_digest(event, cfg)
    existing = db.session.scalar(select(WebhookDelivery).where(
        WebhookDelivery.provider == PROVIDER, WebhookDelivery.event_id == event.event_key))
    if existing is not None:
        if not hmac.compare_digest(existing.payload_digest, digest):
            raise PilotError("tdf_meta_event_conflict", 409)
        return None
    row = WebhookDelivery(provider=PROVIDER, event_id=event.event_key,
        event_type="tdf_sandbox_" + event.kind, payload_digest=digest, status="processing")
    db.session.add(row)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        # Only a genuine unique-key race may replay. Other storage failures
        # must not recurse/retry indefinitely or claim the webhook was stored.
        existing = db.session.scalar(select(WebhookDelivery).where(
            WebhookDelivery.provider == PROVIDER, WebhookDelivery.event_id == event.event_key))
        if existing is None:
            raise PilotError("tdf_meta_receipt_unavailable") from None
        if not hmac.compare_digest(existing.payload_digest, digest):
            raise PilotError("tdf_meta_event_conflict", 409)
        return None
    return row


def _previous_context(contact_key, sender_id):
    row = db.session.scalar(select(MessagingEventLedger).where(
        MessagingEventLedger.tenant_id == TENANT_ID,
        MessagingEventLedger.provider == PROVIDER,
        MessagingEventLedger.provider_sender_id == sender_id,
        MessagingEventLedger.event_type == "tdf_sandbox_reply",
        MessagingEventLedger.request_id == contact_key,
        MessagingEventLedger.external_status.in_(["accepted", "sent", "delivered", "read"])
    ).order_by(MessagingEventLedger.id.desc()).limit(1))
    return (row.metadata_json or {}).get("knowledge_context", {}) if row else {}


def _answer(event, context, *, audio_text=None):
    tenant = db.session.get(TenantProfile, TENANT_ID)
    if tenant is None or tenant.slug != TENANT_SLUG or tenant.is_active is not True:
        raise PilotError("tdf_sandbox_tenant_unavailable")
    owner = db.session.get(User, tenant.municipio_id)
    if owner is None:
        raise PilotError("tdf_sandbox_owner_unavailable")
    session = SimpleNamespace(tenant_id=TENANT_ID, context_data=context)
    if event.content_type == "location":
        return ("📍 Recibí tu ubicación. No la guardaré en esta prueba.\n"
                "¿En qué ciudad necesitás orientación: Ushuaia, Río Grande o Tolhuin?", context, None, ())
    question = audio_text if event.content_type == "audio" else (
        event.selection if event.content_type == "interactive" else event.text)
    if event.content_type == "audio" and not isinstance(audio_text, str):
        raise PilotError("tdf_audio_unavailable")
    if event.content_type == "interactive" and not question.startswith("knowledge:"):
        # The pilot has no operational dispatch, ticket creation or flow submit.
        return ("Esa opción no corresponde al menú de esta prueba. Escribí MENÚ para volver a empezar.", context, None, ())
    if isinstance(question, str) and question.strip().casefold() in {"hola", "buenas", "buen día", "buen dia"}:
        question = "menu"
    result = maybe_handle_institutional_question(question, owner, session)
    if not isinstance(result, dict) or not result.get("message_body"):
        raise PilotError("tdf_sandbox_knowledge_unavailable")
    body = result["message_body"]
    buttons = result.get("botones", [])
    for button in buttons:
        code = button.get("reply_code")
        if code:
            body += "\n" + str(code) + ". " + str(button.get("texto") or "Opción")
    if len(body) > 3500:
        body = body[:3400].rsplit("\n", 1)[0] + "\nInformación completa: https://www.chatboc.ar/t/tierra-del-fuego"
    body += "\n\nPodés responder con el número o tus palabras. Escribí MENÚ para volver."
    return body, session.context_data, result.get("context_revision"), tuple(buttons)


def _reply_payload(recipient, body, buttons, revision):
    """Canonical choices only, with the same numbered text alternative.

    Long/multi-page menus remain text rather than silently hiding options.
    """
    rows = []
    if revision and 0 < len(buttons) <= 10 and len(body) <= 1024:
        for choice in buttons:
            action, label = choice.get("action_id"), choice.get("texto")
            if (not isinstance(action, str) or not action.startswith("knowledge:" + revision[:16] + ":")
                    or not isinstance(label, str) or not label.strip()):
                return cloud.text_payload(recipient=recipient, body=body)
            label = " ".join(label.split())
            code = choice.get("reply_code")
            title = (str(code) + ". " if code else "") + label
            row = {"id": action, "title": title[:24]}
            if len(title) > 24:
                row["description"] = label[:72]
            rows.append(row)
        try:
            return cloud.list_payload(recipient=recipient, body=body, rows=tuple(rows))
        except cloud.MetaContractError:
            pass  # The complete text keeps the published options available.
    return cloud.text_payload(recipient=recipient, body=body)


def _knowledge_revision_current(revision):
    if revision is None:
        return True  # Fixed audio/location/error text contains no canonical content.
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision):
        return False
    # The intent is already committed. Discard cached ORM state before this
    # last public-read check, so retirement/replacement cannot reuse an answer.
    db.session.expire_all()
    with db.session.no_autoflush:
        tenant = db.session.get(TenantProfile, TENANT_ID)
        if tenant is None or tenant.slug != TENANT_SLUG:
            return False
        return read_state(tenant, public=True)["revision"] == revision


def _finish(delivery, status="processed", reason=None):
    delivery.status, delivery.last_error = status, reason
    delivery.processed_at = datetime.now(timezone.utc)
    db.session.commit()


def _status(event, delivery, cfg):
    rows = db.session.scalars(select(MessagingEventLedger).where(
        MessagingEventLedger.tenant_id == TENANT_ID,
        MessagingEventLedger.provider == PROVIDER,
        MessagingEventLedger.provider_sender_id == event.sender.sender_id,
        MessagingEventLedger.external_message_sid == event.message_id,
        MessagingEventLedger.event_type == "tdf_sandbox_reply")).all()
    if len(rows) == 1 and rows[0].request_id == _contact_key(event, cfg):
        row = rows[0]
        ranks = {"accepted": 0, "sent": 1, "delivered": 2, "read": 3, "failed": -1}
        if row.external_status not in {"delivered", "read"} or event.status in {"delivered", "read"}:
            if event.status == "failed" or ranks.get(event.status, -2) >= ranks.get(row.external_status, -2):
                row.external_status = event.status
    _finish(delivery)


def process(raw_body, signature, *, config=None, now=None, authority=None, post=None):
    config = config if config is not None else current_app.config
    cfg = settings(config)
    clock = (lambda: int(time.time())) if now is None else (lambda: now)
    operation_now = clock()
    if cutover_writer_fence_enabled(config):
        raise PilotError("tdf_sandbox_writer_fenced")
    if type(operation_now) is not int or operation_now < 0:
        raise PilotError("tdf_sandbox_clock_invalid")
    verifier = authority or GraphAuthority(cfg, operation_now)
    load = binding_loader(cfg, clock, verifier)
    def resolve(phone, app, waba):
        if (phone, app, waba) != (TEST_PHONE, TEST_APP, TEST_WABA):
            raise PilotError("tdf_sandbox_resource_mismatch", 403)
        return load()
    events = webhook.parse_webhook(raw_body=raw_body, signature=signature,
        app_secret=cfg["APP_SECRET"], app_id=cfg["APP_ID"], now=operation_now,
        binding_resolver=resolve, allow_sandbox_content=True,
        contact_identity_resolver=lambda item, kind, contacts, sender:
            owner_contact_resolver(sender_scope=sender, recipients=cfg["RECIPIENTS"])(item, kind, contacts, sender))
    if len(events) > 10:
        raise PilotError("tdf_meta_batch_limit", 413)
    if any(event.contact not in cfg["RECIPIENTS"] for event in events):
        raise PilotError("tdf_sandbox_recipient_not_allowed", 403)
    replayed, accepted, statuses = 0, 0, 0
    for event in events:
        delivery = _claim(event, cfg)
        if delivery is None:
            replayed += 1
            continue
        if event.kind == "status":
            _status(event, delivery, cfg)
            statuses += 1
            continue
        if not 0 <= clock() - event.timestamp < 24 * 3600:
            _finish(delivery, "failed", "tdf_sandbox_service_window_expired")
            continue
        try:
            contact_key = _contact_key(event, cfg)
            previous = _previous_context(contact_key, event.sender.sender_id)
            if event.content_type == "audio":
                audio_revision = read_state(db.session.get(TenantProfile, TENANT_ID), public=True)["revision"]
                def audio_binding():
                    live_cfg = settings(current_app.config)
                    if (event.contact not in live_cfg["RECIPIENTS"]
                            or cutover_writer_fence_enabled(current_app.config)):
                        raise TdfAudioError("tdf_audio_binding_invalid")
                    return load()
                try:
                    transcript = transcribe_meta_voice_note(event, cfg=cfg, binding_loader=audio_binding, now=clock,
                        metadata_request=_graph_request, metadata_parser=_json_response)
                except TdfAudioError as error:
                    body, context, revision, buttons = audio_error_message(str(error)), previous, None, ()
                else:
                    if not _knowledge_revision_current(audio_revision):
                        raise PilotError("tdf_audio_knowledge_changed")
                    body, context, revision, buttons = _answer(event, previous, audio_text=transcript)
                    if revision != audio_revision:
                        raise PilotError("tdf_audio_knowledge_changed")
            else:
                body, context, revision, buttons = _answer(event, previous)
            outbound = _reply_payload(event.contact, body, buttons, revision)
            # This intent is durable before I/O, and remains uncertain on interruption.
            receipt = MessagingEventLedger(tenant_id=TENANT_ID,
                provider_connection_id=event.sender.connection_id,
                provider_sender_id=event.sender.sender_id, channel="whatsapp", direction="outbound",
                event_type="tdf_sandbox_reply", provider=PROVIDER,
                provider_event_id=event.event_key, external_status="send_uncertain",
                request_id=contact_key, payload=None,
                metadata_json={"contract": CONTRACT, "knowledge_context": context})
            db.session.add(receipt)
            db.session.commit()
            def policy(sender, payload):
                live_cfg = settings(current_app.config)
                return (sender == event.sender and payload == outbound
                        and payload.get("to") == event.contact and event.contact in live_cfg["RECIPIENTS"]
                        and 0 <= clock() - event.timestamp < 24 * 3600
                        and not cutover_writer_fence_enabled(current_app.config)
                        and _knowledge_revision_current(revision))
            result = cloud.send_once(binding_loader=load,
                payload=outbound, now=clock(),
                policy_check=policy, post=post or (lambda url, **kw: _graph_request("POST", url, **kw)))
            receipt.external_status = result.state
            receipt.external_message_sid = result.message_id
            receipt.error_code = str(result.error_code) if result.error_code is not None else None
            _finish(delivery, "processed" if result.state == "accepted" else "failed",
                    None if result.state == "accepted" else "tdf_meta_send_" + result.state)
            accepted += result.state == "accepted"
        except Exception:
            db.session.rollback()
            # Never retry the POST after an exception. Persist only a fixed code.
            persisted = db.session.get(WebhookDelivery, delivery.id)
            if persisted:
                _finish(persisted, "failed", "tdf_sandbox_processing_uncertain")
    return {"contract": CONTRACT, "accepted": accepted, "replayed": replayed, "statuses": statuses,
            "delivery_verified": False}
