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
import unicodedata
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit

from flask import current_app
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from models import db, ConsentLedger, MessagingEventLedger, TenantProfile, User, WebhookDelivery
from services import meta_account_events as account_events
from services import meta_whatsapp_cloud as cloud
from services import meta_whatsapp_webhook as webhook
from services.institutional_assistant import maybe_handle_institutional_question, read_state
from services.institutional_assistant_content import ContentError, is_source_navigation, nearest_locality
from services.institutional_assistant_whatsapp import (
    SOURCE_SCOPE,
    clear_source_scope, present_answer_parts, row_title, short_label, sources_answer, sources_page,
)
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
    raw_mapping = config.get("META_TDF_SANDBOX_REPLY_RECIPIENTS_JSON")
    mapping = {}
    if raw_mapping is not None:
        try:
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate key")
                    result[key] = value
                return result
            if not isinstance(raw_mapping, str) or not 0 < len(raw_mapping.encode("utf-8")) <= 512:
                raise ValueError("bounded JSON required")
            mapping = json.loads(raw_mapping, object_pairs_hook=unique)
            if (not isinstance(mapping, dict) or len(values["RECIPIENTS"]) != 1
                    or set(mapping) != set(values["RECIPIENTS"]) or len(mapping) != 1
                    or any(not isinstance(target, str) or not _CONTACT.fullmatch(target) for target in mapping.values())):
                raise ValueError("one exact owner association required")
        except (ValueError, TypeError, UnicodeError):
            raise PilotError("tdf_sandbox_reply_mapping_invalid") from None
    values["REPLY_RECIPIENTS"] = mapping
    return values


def _reply_recipient(contact, cfg):
    """Inbound canonical identity stays strict; outbound uses only explicit config.

    Deployment prepares this private mapping from Meta's accepted contacts.input
    association. No prefix conversion or inference is performed by the pilot.
    """
    if contact not in cfg["RECIPIENTS"]:
        raise PilotError("tdf_sandbox_recipient_not_allowed", 403)
    return cfg["REPLY_RECIPIENTS"].get(contact, contact)


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


def _compose_menu_answer(parts, buttons):
    """Reserve complete choices/notices before shortening explanatory text."""
    choices = []
    for button in buttons:
        code = button.get("reply_code")
        prefix = str(code) + ". " if code else "- "
        choices.append(prefix + str(button.get("texto") or "Opción"))
    instructions = ("Podés responder con el número o tus palabras. Escribí MENÚ para volver."
                    if any(button.get("reply_code") for button in buttons) else
                    "Podés escribir tu consulta con tus palabras. Escribí MENÚ para volver.")
    reserved = "\n\n".join(part for part in (parts["notices"], "\n".join(choices), instructions) if part)
    if len(reserved) > 4096:
        return None
    complete = "\n\n".join(part for part in (parts["text"], *parts["links"], reserved) if part)
    acronyms = parts.get("acronyms") or ""
    if acronyms:
        # «Siglas» helps reading, but never costs the interactive list (body
        # up to 1024) nor a complete answer: it is the first part to give way.
        explained = "\n\n".join(part for part in (parts["text"], acronyms, *parts["links"], reserved) if part)
        if len(explained) <= (1024 if len(complete) <= 1024 else 4096):
            return explained
    if len(complete) <= 4096:
        return complete
    # Links are atomic: none can become a partial URL after abbreviation.
    more = "Información completa: https://www.chatboc.ar/t/tierra-del-fuego"
    suffix = more + "\n\n" + reserved
    budget = 4096 - len(suffix) - 2
    if budget < 0:
        return None
    text = parts["text"][:budget]
    if len(text) < len(parts["text"]) and text and not text[-1].isspace():
        text = re.sub(r"\S+$", "", text)
    return "\n\n".join(part for part in (text.rstrip(), suffix) if part)


NEWS_FLAG = "META_TDF_SANDBOX_NEWS_OPT_IN_ENABLED"
FLOW_MENU = {"tdf_flow": "CONSULTA"}
FLOW_SCREEN = "CONSULTA"
FLOW_CTA = "Elegir ciudad y tema"


def _slug(value):
    from services.institutional_assistant_content import _plain
    return re.sub(r"[^a-z0-9]+", "-", _plain(value)).strip("-")


def _flow_choice(tenant, selection, context):
    """City and topic chosen in the Flow, checked against the published knowledge."""
    try:
        chosen = json.loads(selection[len("flow:"):])
        state = read_state(tenant, public=True)
    except (ValueError, ContentError):
        return "menu", clear_source_scope(context)
    if not isinstance(chosen, dict) or not state:
        return "menu", clear_source_scope(context)
    next_context = dict(context) if isinstance(context, dict) else {}
    place = next((item["label"] for item in state["bundle"].get("localities") or []
                  if _slug(item["label"]) == chosen.get("ciudad")), None)
    if place is not None:
        knowledge = next_context.get("institutional_knowledge")
        next_context["institutional_knowledge"] = {**(knowledge if isinstance(knowledge, dict) else {}), "locality": place}
    topic = chosen.get("tema")
    if not isinstance(topic, str) or topic not in state["bundle"]["nodes"]:
        return "menu", next_context
    return "knowledge:" + state["revision"][:16] + ":" + topic, next_context


FEEDBACK_HINT = "¿Te sirvió esta respuesta? Respondé SÍ o NO."
FEEDBACK_PENDING = "institutional_feedback_pending"


def _answer_feedback(event, context, tenant):
    """A SÍ or NO right after an answer that asked for it: counted once, anonymously.

    Only the topic, the knowledge revision and yes/no are kept (as on the web)."""
    pending = context.get(FEEDBACK_PENDING) if isinstance(context, dict) else None
    if event.content_type != "text" or not isinstance(pending, dict):
        return None
    word = " ".join(str(event.text or "").split()).casefold().strip(".!¡")
    helpful = {"si": True, "sí": True, "no": False}.get(word)
    if helpful is None:
        return None
    from services.institutional_assistant_metrics import record_answer_feedback
    record_answer_feedback(tenant.id, tenant.tipo, "whatsapp", pending.get("revision"), pending.get("node_id"), helpful)
    next_context = dict(context)
    next_context.pop(FEEDBACK_PENDING, None)
    if helpful:
        return "Gracias por avisarnos. Escribí MENÚ para ver otros temas.", next_context, None, ()
    return ("Gracias por avisarnos. Lo vamos a revisar.\n"
            "Si necesitás hablar con una oficina, escribí 0 para ver los contactos por ciudad, o MENÚ para volver a los temas.",
            next_context, None, ())


NEWS_HINT = "📣 Para recibir avisos de eventos y trámites, escribí NOVEDADES."
_NEWS_COMMANDS = {"novedades": "ask", "alta": "opt_in", "baja": "opt_out"}


def _news_consent(event, context):
    """Explicit sign-up and sign-off for news, off unless the organization enables it.

    Only the exact commands count. Signing up is the single point where this
    pilot keeps a phone number, and only because the person asked to be written
    to; signing off is honoured at once and recorded the same way."""
    if current_app.config.get(NEWS_FLAG) is not True or event.content_type != "text":
        return None
    command = _NEWS_COMMANDS.get(" ".join(str(event.text or "").split()).casefold().strip(".!¡"))
    if command is None:
        return None
    if command == "ask":
        return ("Podemos avisarte por WhatsApp sobre eventos y trámites de discapacidad.\n"
                "Para recibir esos avisos, escribí ALTA. Podés dejar de recibirlos cuando quieras con BAJA.",
                clear_source_scope(context), None, ())
    db.session.add(ConsentLedger(tenant_id=TENANT_ID, contact_key=event.contact, channel="whatsapp",
        status=command, source="tdf_whatsapp_assistant",
        evidence_payload={"contract": CONTRACT, "event": event.event_key, "command": command}))
    db.session.commit()
    if command == "opt_in":
        return ("Listo. Te vamos a avisar por este WhatsApp sobre eventos y trámites.\n"
                "Para dejar de recibir avisos, escribí BAJA.", clear_source_scope(context), None, ())
    return ("Listo. No vas a recibir más avisos. Podés seguir consultando cuando quieras.",
            clear_source_scope(context), None, ())


def news_opted_in(contact):
    """Whether the latest recorded choice of this number is to receive news."""
    row = db.session.scalar(select(ConsentLedger).where(
        ConsentLedger.tenant_id == TENANT_ID, ConsentLedger.channel == "whatsapp",
        ConsentLedger.source == "tdf_whatsapp_assistant", ConsentLedger.contact_key == contact,
    ).order_by(ConsentLedger.id.desc()).limit(1))
    return row is not None and row.status == "opt_in"


_CLAIM_STATUS = re.compile(r"\b(estado|seguimiento|seguir|c[oó]mo (va|sigue|viene)|novedad(es)? (de|del|sobre)|"
                           r"consultar|ver|respuesta (de|del|a))\b", re.IGNORECASE)
_CLAIM_NUMBER = re.compile(r"\b(?:[ms]-?)?(\d{5,12})\b", re.IGNORECASE)
# The portal's own form hands out «T-12»: the letter is part of the code.
_CLAIM_RECEIPT = re.compile(r"\bT[-\s]?(\d{1,10})\b", re.IGNORECASE)
CLAIM_TRACKING_URL = "https://tdf.chatboc.ar/tracking/claim"


def _claim_tracking(text):
    """Where to follow a complaint already filed.

    Only a message about «reclamo» that asks for its status or gives its
    number: how to file one stays a knowledge question. The status itself is
    never shown in the chat; the page asks for the PIN the person received."""
    if not isinstance(text, str) or len(text) > 160 or not re.search(r"\breclamos?\b", text, re.IGNORECASE):
        return None
    receipt, number = _CLAIM_RECEIPT.search(text), _CLAIM_NUMBER.search(text)
    code = "T-" + receipt.group(1) if receipt else number.group(1) if number else None
    if code is None and not _CLAIM_STATUS.search(text):
        return None
    if re.search(r"\b(hacer|iniciar|cargar|presentar|nuevo|d[oó]nde)\b", text, re.IGNORECASE) and code is None:
        return None
    link = CLAIM_TRACKING_URL + ("/" + code if code else "")
    opening = ("Para ver cómo va el reclamo " + code + ", abrí este enlace:" if code
               else "Para ver cómo va tu reclamo, abrí este enlace y escribí el número que recibiste al cargarlo:")
    return (opening + "\n" + link + "\n\n"
            "Te va a pedir el PIN que recibiste junto con el número. "
            "Por tu privacidad, el estado no se muestra en este chat.\n\n"
            "Escribí MENÚ para volver a los temas.")


def _published_choice(tenant, action):
    """A row tapped in an older message keeps working after the knowledge is
    updated: the same topic is answered from the published version, never from
    the old one. A topic that no longer exists opens the menu."""
    parts = action.split(":")
    try:
        state = read_state(tenant, public=True)
    except ContentError:
        return action
    current = state["revision"][:16]
    if len(parts) != 3 or parts[1] == current or parts[1] == "sources":
        return action
    return "knowledge:" + current + ":" + parts[2] if parts[2] in state["bundle"]["nodes"] else "menu"


def _answer(event, context, *, audio_text=None):
    tenant = db.session.get(TenantProfile, TENANT_ID)
    if tenant is None or tenant.slug != TENANT_SLUG or tenant.is_active is not True:
        raise PilotError("tdf_sandbox_tenant_unavailable")
    owner = db.session.get(User, tenant.municipio_id)
    if owner is None:
        raise PilotError("tdf_sandbox_owner_unavailable")
    session = SimpleNamespace(tenant_id=TENANT_ID, context_data=context)
    feedback = _answer_feedback(event, context, tenant)
    if feedback is not None:
        return feedback
    news = _news_consent(event, context)
    if news is not None:
        return news
    if event.content_type == "location":
        next_context = clear_source_scope(context)
        # The pin only chooses the city of this conversation. Coordinates are
        # compared here and dropped; the context keeps the city's name.
        place = None
        position = getattr(event, "location", None)
        if position is not None:
            try:
                state = read_state(tenant, public=True)
                place = nearest_locality(state["bundle"], *position) if state else None
            except ContentError:
                place = None
        if place is None:
            return ("📍 Recibí tu ubicación. No la guardo.\n"
                    "¿En qué ciudad necesitás orientación: Ushuaia, Río Grande o Tolhuin?", next_context, None, ())
        knowledge = next_context.get("institutional_knowledge")
        next_context["institutional_knowledge"] = {**(knowledge if isinstance(knowledge, dict) else {}), "locality": place}
        return ("📍 Recibí tu ubicación: estás cerca de " + place + ". No la guardo.\n"
                "Te voy a mostrar primero lo de esa ciudad. Escribí tu consulta o MENÚ para ver los temas.",
                next_context, None, ())
    question = audio_text if event.content_type == "audio" else (
        event.selection if event.content_type == "interactive" else event.text)
    if event.content_type == "audio" and not isinstance(audio_text, str):
        raise PilotError("tdf_audio_unavailable")
    if event.content_type == "interactive" and question.startswith("flow:"):
        question, context = _flow_choice(tenant, question, context)
        session.context_data = context
    if event.content_type == "interactive" and not question.startswith("knowledge:") and question != "menu":
        # The pilot has no operational dispatch, ticket creation or flow submit.
        return ("Esa opción no corresponde al menú de esta prueba. Escribí MENÚ para volver a empezar.", clear_source_scope(context), None, ())
    if event.content_type == "interactive":
        question = _published_choice(tenant, question)
    if event.content_type == "text":
        tracking = _claim_tracking(question)
        if tracking is not None:
            return tracking, clear_source_scope(context), None, ()
    page = sources_page(question)
    if page is None and isinstance(question, str) and is_source_navigation(question):
        # Natural-language requests for the references use the pilot's own
        # public-only catalogue of the last orientation, like FUENTES.
        page = 1
    if page is not None:
        body, next_context, revision = sources_answer(tenant, context, page)
        return body + "\n\nEscribí MENÚ para volver a los temas.", next_context, revision, ()
    if isinstance(question, str) and question.strip().casefold() in {"hola", "buenas", "buen día", "buen dia"}:
        question = "menu"
    result = maybe_handle_institutional_question(question, owner, session)
    if result is None:
        # An operational command (for example "iniciar un reclamo") belongs to
        # flows this pilot does not run; answer instead of failing the delivery.
        return ("Esa opción no corresponde al menú de esta prueba. Escribí MENÚ para volver a empezar.",
                clear_source_scope(context), None, ())
    if not isinstance(result, dict) or not result.get("message_body"):
        raise PilotError("tdf_sandbox_knowledge_unavailable")
    parts, session.context_data = present_answer_parts(result, session.context_data,
        tenant_id=TENANT_ID, tenant_slug=TENANT_SLUG)
    buttons = result.get("botones", [])
    body = _compose_menu_answer(parts, buttons)
    if body is None:
        session.context_data = clear_source_scope(session.context_data)
        session.context_data.pop("institutional_knowledge", None)
        return ("No pude mostrar todas las opciones de esta orientación en un solo mensaje. "
                "Escribí MENÚ para elegir un tema nuevamente.", session.context_data,
                result.get("context_revision"), ())
    if (current_app.config.get(NEWS_FLAG) is True and isinstance(question, str)
            and question.strip().casefold() in {"menu", "menú", "inicio"}
            and len(body) + len(NEWS_HINT) + 2 <= (1024 if len(body) <= 1024 else 4096)):
        # Shown on the opening menu only, and never at the cost of the list.
        body += "\n\n" + NEWS_HINT
    knowledge = session.context_data.get("institutional_knowledge")
    flow_id = current_app.config.get("META_TDF_SANDBOX_FLOW_ID") or ""
    if (flow_id and isinstance(question, str) and question.strip().casefold() in {"menu", "menú", "inicio"}
            and not (isinstance(knowledge, dict) and knowledge.get("locality")) and len(body) <= 1024):
        # First contact: city and topic in one screen. The numbered text keeps working.
        buttons = [FLOW_MENU]
    session.context_data.pop(FEEDBACK_PENDING, None)
    scope = session.context_data.get(SOURCE_SCOPE)
    if (isinstance(question, str) and question.strip().casefold() not in {"menu", "menú", "inicio"}
            and isinstance(scope, dict) and scope.get("node_ids") and result.get("context_revision")
            and scope.get("revision") == result.get("context_revision")
            and len(body) + len(FEEDBACK_HINT) + 2 <= (1024 if len(body) <= 1024 else 4096)):
        # The question goes last, and never at the cost of the list or the answer.
        body += "\n\n" + FEEDBACK_HINT
        session.context_data[FEEDBACK_PENDING] = {"revision": scope["revision"], "node_id": scope["node_ids"][0]}
    return body, session.context_data, result.get("context_revision"), tuple(buttons)


def _reply_payload(recipient, body, buttons, revision):
    """Canonical choices only, with the same numbered text alternative.

    Long/multi-page menus remain text rather than silently hiding options.
    """
    if tuple(buttons) == (FLOW_MENU,):
        flow_id = current_app.config.get("META_TDF_SANDBOX_FLOW_ID") or ""
        try:
            return cloud.flow_payload(recipient=recipient, body=body, flow_id=flow_id, cta=FLOW_CTA,
                token="tdf-consulta", screen=FLOW_SCREEN,
                draft=current_app.config.get("META_TDF_SANDBOX_FLOW_DRAFT") is True)
        except cloud.MetaContractError:
            return cloud.text_payload(recipient=recipient, body=body)
    rows, titles = [], set()
    if revision and 0 < len(buttons) <= 10 and len(body) <= 1024:
        for choice in buttons:
            action, label = choice.get("action_id"), choice.get("texto")
            if (not isinstance(action, str) or not action.startswith("knowledge:" + revision[:16] + ":")
                    or not isinstance(label, str) or not label.strip()):
                return cloud.text_payload(recipient=recipient, body=body)
            label = " ".join(label.split())
            code = choice.get("reply_code")
            prefix = str(code) + ". " if code else ""
            if len(prefix) >= 24:
                return cloud.text_payload(recipient=recipient, body=body)
            title = prefix + label
            row = {"id": action, "title": row_title(prefix, label)}
            if row["title"] in titles:
                # The complete numbered body remains unambiguous when two
                # unnumbered labels shorten to the same title.
                return cloud.text_payload(recipient=recipient, body=body)
            titles.add(row["title"])
            if len(title) > 24:
                row["description"] = short_label(label, 72)
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


def _handout(context, revision):
    """The one PDF the last answer hands out, or None.

    The answer may join up to three topics (a question in the person's own
    words often does); the first published PDF among them is the one sent.
    Only the current published revision, and only a source the organization
    marked public. Anything else sends no document."""
    scope = context.get(SOURCE_SCOPE) if isinstance(context, dict) else None
    if (not revision or not isinstance(scope, dict) or scope.get("revision") != revision
            or not isinstance(scope.get("node_ids"), list) or not 1 <= len(scope["node_ids"]) <= 3):
        return None
    tenant = db.session.get(TenantProfile, TENANT_ID)
    state = read_state(tenant, public=True)
    if state is None or state["revision"] != revision:
        return None
    sources = [source for node_id in scope["node_ids"]
               for source in (state["bundle"]["nodes"].get(node_id) or {}).get("sources", [])]
    for source in sources:
        if source.get("document_visibility") == "public" and source.get("format", "pdf") == "pdf":
            title = " ".join(str(source.get("title", "")).split())
            name = re.sub(r"[^A-Za-z0-9 ._()-]", "", unicodedata.normalize("NFKD", title)
                          .encode("ascii", "ignore").decode())[:100].strip(" .") or "documento"
            return {"link": "https://api.chatboc.ar/api/public/tenants/" + TENANT_SLUG
                            + "/institutional-assistant/sources/" + source["id"] + "?revision=" + revision,
                    "filename": name + ".pdf", "caption": title[:1024] or None}
    return None


def _send_handout(event, context, revision, recipient, cfg, contact_key, load, clock, post):
    """Second message after an accepted reply: the published document itself.

    It has its own ledger row and never changes the outcome of the reply."""
    item = _handout(context, revision)
    if item is None:
        return
    outbound = cloud.document_payload(recipient=recipient, **item)
    receipt = MessagingEventLedger(tenant_id=TENANT_ID, provider_connection_id=event.sender.connection_id,
        provider_sender_id=event.sender.sender_id, channel="whatsapp", direction="outbound",
        event_type="tdf_sandbox_document", provider=PROVIDER, provider_event_id=event.event_key + ":document",
        external_status="send_uncertain", request_id=contact_key, payload=None,
        metadata_json={"contract": CONTRACT})
    db.session.add(receipt)
    db.session.commit()

    def policy(sender, payload):
        live_cfg = settings(current_app.config)
        return (sender == event.sender and payload == outbound
                and live_cfg["REPLY_RECIPIENTS"] == cfg["REPLY_RECIPIENTS"]
                and live_cfg["RECIPIENTS"] == cfg["RECIPIENTS"]
                and event.contact in live_cfg["RECIPIENTS"]
                and payload.get("to") == recipient == _reply_recipient(event.contact, live_cfg)
                and 0 <= clock() - event.timestamp < 24 * 3600
                and not cutover_writer_fence_enabled(current_app.config)
                and _knowledge_revision_current(revision)
                and _handout(context, revision) == item)
    result = cloud.send_once(binding_loader=load, payload=outbound, now=clock(), policy_check=policy,
        post=post or (lambda url, **kw: _graph_request("POST", url, **kw)))
    receipt.external_status = result.state
    receipt.external_message_sid = result.message_id
    receipt.error_code = str(result.error_code) if result.error_code is not None else None
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
    # Account and template notifications always reach the application callback
    # (Meta does not route them through per-number overrides) and carry no
    # chat. Once the signature is verified they are acknowledged and nothing
    # else happens: no tenant is resolved, nothing is stored, nobody is
    # answered. Any other delivery, mixed batches included, continues below
    # unchanged.
    acknowledged = account_events.acknowledged_account_events(
        raw_body=raw_body, signature=signature, app_secret=cfg["APP_SECRET"])
    if acknowledged is not None:
        return {"result": account_events.ACKNOWLEDGED_RESULT,
                "events": sum(acknowledged.values()),
                "fields": dict(sorted(acknowledged.items()))}
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
                    body, context, revision, buttons = audio_error_message(str(error)), clear_source_scope(previous), None, ()
                else:
                    if not _knowledge_revision_current(audio_revision):
                        raise PilotError("tdf_audio_knowledge_changed")
                    body, context, revision, buttons = _answer(event, previous, audio_text=transcript)
                    if revision != audio_revision:
                        raise PilotError("tdf_audio_knowledge_changed")
            else:
                body, context, revision, buttons = _answer(event, previous)
            outbound_recipient = _reply_recipient(event.contact, cfg)
            outbound = _reply_payload(outbound_recipient, body, buttons, revision)
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
                        and live_cfg["REPLY_RECIPIENTS"] == cfg["REPLY_RECIPIENTS"]
                        and live_cfg["RECIPIENTS"] == cfg["RECIPIENTS"]
                        and event.contact in live_cfg["RECIPIENTS"]
                        and payload.get("to") == outbound_recipient == _reply_recipient(event.contact, live_cfg)
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
            if result.state == "accepted":
                # The reply is already final. A document that cannot be sent
                # leaves the conversation exactly as the reply left it.
                try:
                    _send_handout(event, context, revision, outbound_recipient, cfg, contact_key, load, clock, post)
                except Exception:
                    db.session.rollback()
        except Exception:
            db.session.rollback()
            # Never retry the POST after an exception. Persist only a fixed code.
            persisted = db.session.get(WebhookDelivery, delivery.id)
            if persisted:
                _finish(persisted, "failed", "tdf_sandbox_processing_uncertain")
    return {"contract": CONTRACT, "accepted": accepted, "replayed": replayed, "statuses": statuses,
            "delivery_verified": False}
