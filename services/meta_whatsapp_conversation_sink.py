"""Explicit server sink, not a converter from Twilio template payloads.

The native welcome/receipt producers must be called explicitly by server code.
Existing raw _twilio_pre_messages remain HOLD; they are never discarded here.
"""
from collections.abc import Mapping

from flask import current_app

from models import MunicipioTicket, TenantProfile, db
from services.contact_service import current_contact_phone
from services.meta_whatsapp_media import (
    canonical_configured_media_url, load_verified_media_reference, revalidate_media_reference,
)
from services.meta_whatsapp_outbox import (
    MetaReplyContent, MetaReplyBundle, _reply_messages, _identity, _inbound_reference,
)
from services.meta_whatsapp_runtime import MetaRuntimeError
from services.meta_whatsapp_cloud import MetaContractError
from services.tenant_provider_credentials import ProviderCredentialError
from services.tenant_ticket_scope import scoped_municipio_ticket_query
from services.whatsapp_conversation_uow import ConversationUnitOfWork
from services.whatsapp_receipt_branding import resolve_whatsapp_receipt_header
from services.whatsapp_receipts import build_claim_created_template_pre_message, build_claim_created_followup_text


class MetaConversationSink:
    enabled = True

    def __init__(self, unit):
        if not isinstance(unit, ConversationUnitOfWork):
            raise MetaRuntimeError("meta_sink_server_unit_required")
        self.unit, self._items = unit, []
        self._scope()

    def _scope(self):
        claim = self.unit.claim
        self.unit.require_writer_scope(session=db.session(), tenant_id=claim.tenant_id, turn_id=claim.turn_id)
        record = _inbound_reference(claim.payload, self.unit.clock())
        identity = _identity(record["identity"])
        recipient = self.unit.contact_identity[1:]
        if record["source"]["recipient"] != recipient:
            self.unit.fail("meta_sink_recipient_mismatch")
        return identity, recipient

    def _extend(self, additions):
        identity, recipient = self._scope()
        try:
            candidate = MetaReplyBundle(tuple(self._items) + tuple(additions))
            _reply_messages(candidate, recipient)  # Every item before changing the collector.
            for item in candidate.messages:
                if item.media is not None:
                    revalidate_media_reference(item.media, identity, session=db.session(), now=self.unit.clock())
        except (MetaContractError, ProviderCredentialError) as error:
            self.unit.fail(error.code if isinstance(error, MetaRuntimeError) else "meta_conversation_output_invalid")
        self._items = list(candidate.messages)

    def emit_text(self, body):
        self._extend((MetaReplyContent("text", body),))

    def _media(self, role, configured_url):
        identity, _ = self._scope()
        try:
            reference = load_verified_media_reference(identity, role, session=db.session(), now=self.unit.clock())
            # Full current URL must match the trusted server registration's
            # URL -> local SHA -> upload receipt/MediaID binding. No path-only
            # origin substitution, URL fetch or request Host/Origin fallback.
            selected_url = canonical_configured_media_url(configured_url,
                static_path=reference.static_path,
                public_base_url=current_app.config.get("APP_BASE_URL") or "")
            if selected_url != reference.configured_url:
                raise MetaRuntimeError("meta_configured_media_unverified")
        except (MetaRuntimeError, ValueError) as error:
            self.unit.fail(error.code if isinstance(error, MetaRuntimeError) else "meta_configured_media_unverified")
        return MetaReplyContent("sticker" if role == "welcome_sticker" else "image", "", media=reference)

    def emit_welcome(self, *, greeting, menu: MetaReplyContent, configured_sticker_url=None):
        """Explicit current greeting -> configured sticker -> complete menu.

        Greeting/menu come from the existing server welcome producer. No intent
        recognition, ContentSid mapping, delayed send or template approval here.
        A configured sticker without a current verified MediaRef poisons Unit.
        """
        additions = [MetaReplyContent("text", greeting)]
        if configured_sticker_url:
            additions.append(self._media("welcome_sticker", configured_sticker_url))
        if not isinstance(menu, MetaReplyContent) or not isinstance(menu.kind, str) or menu.kind not in {"text", "interactive_list"}:
            self.unit.fail("meta_welcome_menu_projection_required")
        additions.append(menu)
        self._extend(additions)

    def emit_claim_receipt(self, *, ticket_id, callback_requested=False, text_only_followup=False):
        """Read the current persisted municipal ticket; never accept its PIN from LLM JSON.

        This server-only producer uses the same semantic receipt/followup factory,
        not a ContentSid/template conversion. Callback flag must come from the
        existing server business result that recorded the request.
        """
        identity, _ = self._scope()
        if (type(ticket_id) is not int or ticket_id <= 0 or type(callback_requested) is not bool
                or type(text_only_followup) is not bool):
            self.unit.fail("meta_receipt_ticket_invalid")
        with db.session.no_autoflush:
            tenant = db.session.get(TenantProfile, identity.tenant_id)
            ticket = scoped_municipio_ticket_query(tenant).with_entities(
                MunicipioTicket.id, MunicipioTicket.nro_ticket, MunicipioTicket.consulta_pin,
                MunicipioTicket.categoria, MunicipioTicket.telefono_vecino, MunicipioTicket.anon_id
            ).filter(MunicipioTicket.id == ticket_id).one_or_none()
            cached = db.session().identity_map.get(db.session().identity_key(class_=MunicipioTicket, ident=(ticket_id,)))
            if (ticket is None or (cached is not None and (cached in db.session.dirty or cached in db.session.deleted))
                    or current_contact_phone(ticket.telefono_vecino, ticket.anon_id) != self.unit.contact_identity
                    or not isinstance(ticket.consulta_pin, str) or len(ticket.consulta_pin) != 6
                    or not ticket.consulta_pin.isdigit() or not ticket.nro_ticket):
                self.unit.fail("meta_receipt_ticket_scope_invalid")
            code = str(ticket.nro_ticket).strip()
            if not code.upper().startswith("M-"):
                code = "M-" + code
            base = current_app.config.get("APP_BASE_URL") or "https://www.chatboc.ar/chat"
            semantic = build_claim_created_template_pre_message(ticket_nro=code, categoria=ticket.categoria,
                consulta_pin=ticket.consulta_pin, base_chat_url=base, within_24h_window=True)
            followup = build_claim_created_followup_text(ticket.consulta_pin,
                callback_requested=callback_requested, text_only_followup=text_only_followup)
            header = resolve_whatsapp_receipt_header(tenant, public_base_url=base)
            config = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
            explicit = config.get("whatsapp_receipt_header")
            if explicit is not None and (not isinstance(explicit, dict) or type(explicit.get("enabled", True)) is not bool):
                self.unit.fail("meta_configured_header_unverified")
            if (isinstance(explicit, dict) and explicit.get("enabled", True) is True and not header):
                self.unit.fail("meta_configured_header_unverified")
        additions = [self._media("receipt_header", header)] if header else []
        # Keep the full primary receipt (tracking with PIN) and its distinct
        # followup. No caption limit, truncation or primary-message omission.
        additions.extend((MetaReplyContent("text", semantic["body"]), MetaReplyContent("text", followup)))
        self._extend(additions)

    def emit_response(self, bot_response: Mapping, formatted_response: Mapping):
        self._scope()
        if not isinstance(bot_response, Mapping) or not isinstance(formatted_response, Mapping):
            self.unit.fail("meta_response_projection_invalid")
        if bot_response.get("_twilio_pre_messages") or formatted_response.get("_twilio_pre_messages"):
            self.unit.fail("meta_twilio_pre_messages_require_native_projection")
        media_fields = ("image_url", "audio_url", "sticker_url", "document_url", "video_url", "media_url",
                        "_welcome_sticker_urls", "_preserve_welcome_header")
        if any(bot_response.get(key) or formatted_response.get(key) for key in media_fields):
            self.unit.fail("meta_media_requires_server_projection")
        kind = formatted_response.get("type")
        if kind == "text":
            text = formatted_response.get("text")
            if not isinstance(text, Mapping):
                self.unit.fail("meta_response_projection_invalid")
            self.emit_text(text.get("body"))
        elif kind == "interactive":
            item = formatted_response.get("interactive") or {}
            if not isinstance(item, Mapping) or not isinstance(item.get("action"), Mapping) or not isinstance(item.get("body"), Mapping):
                self.unit.fail("meta_response_projection_invalid")
            sections = (item.get("action") or {}).get("sections")
            if (item.get("type") != "list" or not isinstance(sections, list) or len(sections) != 1
                    or not isinstance(sections[0], Mapping)
                    or item.get("header") or item.get("footer")
                    or (item.get("action") or {}).get("button") != "Elegir un tema"
                    or sections[0].get("title") != "Opciones"):
                self.unit.fail("meta_response_projection_required")
            self._extend((MetaReplyContent("interactive_list", (item.get("body") or {}).get("text"),
                                          rows=tuple(sections[0].get("rows") or ())),))
        else:
            self.unit.fail("meta_response_projection_required")

    def bundle(self):
        self._extend(())  # Revalidate complete order/current references before finalization.
        return MetaReplyBundle(tuple(self._items))
