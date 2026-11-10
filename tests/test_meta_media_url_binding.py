"""PREP only: real sink/catalog/Unit with a synthetic registration proof.

Synthetic host names are test data, never a production allowlist or evidence of
remote hosting/Meta upload. No URL is downloaded and no provider is activated.
"""
from dataclasses import replace
import copy

import pytest

from models import MunicipioTicket, WhatsAppOutboundAttempt, db
from services.meta_whatsapp_conversation_sink import MetaConversationSink
from services.meta_whatsapp_media import UPLOAD_PROOF_VERIFIER
from services.meta_whatsapp_outbox import MetaReplyContent
from services.whatsapp_conversation_uow import ConversationUnitOfWorkError
from tests.test_whatsapp_conversation_uow import environment, unit_env, _unit
from tests.test_meta_native_media_output import media_env, _begin, _catalog


def _welcome(sink, configured_url):
    sink.emit_welcome(greeting="Hola sintético", menu=MetaReplyContent("text", "Menú completo"),
        configured_sticker_url=configured_url)


def _own_ticket(env):
    ticket = MunicipioTicket(tenant_id=env.tenant.id, municipio_id=env.owner.id,
        nro_ticket="219612", consulta_pin="654321", categoria="Arbolado",
        telefono_vecino="+" + env.event.contact, anon_id="+" + env.event.contact)
    db.session.add(ticket)
    db.session.commit()
    ticket_id = ticket.id
    _begin(env)
    return ticket_id


@pytest.mark.parametrize("case", ("other-host", "other-scheme", "query", "fragment", "protocol-relative"))
def test_same_static_path_does_not_authorize_another_configured_url(media_env, case):
    env = media_env
    url = {
        "other-host": "https://different.example.test/static/synthetic/sticker.webp",
        "other-scheme": "http://assets.example.test/static/synthetic/sticker.webp",
        "query": env.sticker_url + "?brand=different",
        "fragment": env.sticker_url + "#different-brand",
        "protocol-relative": "//assets.example.test/static/synthetic/sticker.webp",
    }[case]
    with pytest.raises(ConversationUnitOfWorkError, match="meta_configured_media_unverified"):
        with _unit(env) as unit:
            _welcome(MetaConversationSink(unit), url)
    assert WhatsAppOutboundAttempt.query.count() == 0
    assert env.calls["post"] == [] and env.calls["twilio"] == 0


def test_real_receipt_producer_rejects_header_origin_collision(media_env):
    env = media_env
    env.tenant.configuracion = {"whatsapp_receipt_header": {
        "enabled": True, "image_url": "https://different.example.test/static/synthetic/receipt.png"}}
    db.session.commit()
    ticket_id = _own_ticket(env)
    with pytest.raises(ConversationUnitOfWorkError, match="meta_configured_media_unverified"):
        with _unit(env) as unit:
            MetaConversationSink(unit).emit_claim_receipt(ticket_id=ticket_id)
    assert MunicipioTicket.query.count() == 1  # Existing case is neither rewritten nor deleted.
    assert WhatsAppOutboundAttempt.query.count() == 0 and env.calls["post"] == []


def test_registration_proof_must_bind_the_same_complete_url(media_env):
    env = media_env
    original = env.app.extensions[UPLOAD_PROOF_VERIFIER]
    env.app.extensions[UPLOAD_PROOF_VERIFIER] = lambda ref, identity, **kwargs: replace(
        original(ref, identity, **kwargs),
        configured_url="https://different.example.test/static/synthetic/sticker.webp")
    with pytest.raises(ConversationUnitOfWorkError, match="meta_media_upload_proof_invalid"):
        with _unit(env) as unit:
            _welcome(MetaConversationSink(unit), env.sticker_url)
    assert WhatsAppOutboundAttempt.query.count() == 0 and env.calls["post"] == []


@pytest.mark.parametrize("case", ("absolute", "canonical-alias", "relative-server-base"))
def test_exact_registered_legacy_url_preserves_order_without_request_origin_authority(media_env, case):
    env = media_env
    configured = env.sticker_url
    if case == "canonical-alias":
        configured = "https://ASSETS.EXAMPLE.TEST:443/static/synthetic/sticker.webp"
    elif case == "relative-server-base":
        env.app.config["APP_BASE_URL"] = "https://assets.example.test"
        configured = "/static/synthetic/sticker.webp"
    # Caller-controlled request origin must not select the catalog or base URL.
    with env.app.test_request_context(base_url="https://unrelated-request.example.test"):
        with _unit(env) as unit:
            sink = MetaConversationSink(unit)
            _welcome(sink, configured)
            bundle = sink.bundle()
            assert [item.kind for item in bundle.messages] == ["text", "sticker", "text"]
            assert bundle.messages[1].media.configured_url == env.sticker_url
            assert bundle.messages[1].media.media_id == "400002"
            assert bundle.messages[2].body == "Menú completo"
            unit.commit_reply(bundle, runtime=env.runtime)
    assert WhatsAppOutboundAttempt.query.count() == 3
    assert env.calls["post"] == [] and env.calls["twilio"] == 0


def test_relative_legacy_url_cannot_guess_the_registered_origin(media_env):
    env = media_env
    env.app.config["APP_BASE_URL"] = "https://different.example.test"
    with pytest.raises(ConversationUnitOfWorkError, match="meta_configured_media_unverified"):
        with _unit(env) as unit:
            _welcome(MetaConversationSink(unit), "/static/synthetic/sticker.webp")
    assert WhatsAppOutboundAttempt.query.count() == 0 and env.calls["post"] == []


def test_old_catalog_without_explicit_url_binding_is_not_silently_upgraded(media_env):
    env = media_env
    records = copy.deepcopy(env.records)
    records["welcome_sticker"].pop("configured_url")
    _catalog(env, records)
    with pytest.raises(ConversationUnitOfWorkError, match="meta_media_reference_invalid"):
        with _unit(env) as unit:
            _welcome(MetaConversationSink(unit), env.sticker_url)
    assert WhatsAppOutboundAttempt.query.count() == 0 and env.calls["post"] == []
