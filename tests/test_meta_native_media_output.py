"""New proposed native-media controls; real ORM/Unit/outbox/worker, fake Graph.

No tests from the closed Meta outbox module are imported or collected. Catalog
upload receipts/media IDs are explicitly synthetic and install no provider data.
"""
import copy
from dataclasses import replace
import hashlib
import random

from PIL import Image
import pytest
from sqlalchemy import text

from models import User, TenantProfile, MunicipioTicket, WhatsAppInboundTurn, WhatsAppOutboundAttempt, db
from services import meta_whatsapp_cloud as cloud, whatsapp_inbound_worker as worker
from services.meta_whatsapp_conversation_sink import MetaConversationSink
from services.meta_whatsapp_media import CATALOG_KEY, CONTRACT, UPLOAD_PROOF_VERIFIER, VerifiedMediaUpload, load_verified_media_reference
from services.meta_whatsapp_outbox import RUNTIME_EXTENSION, MetaReplyContent, MetaReplyBundle, produce_meta_service_reply
from services.meta_whatsapp_runtime import MetaRuntimeError
from services.outbox_execution_budget import OutboxExecutionBudgetExceeded
from services.whatsapp_conversation_uow import ConversationUnitOfWorkError
from tests.test_whatsapp_conversation_uow import environment, unit_env, _unit


@pytest.fixture
def media_env(unit_env, tmp_path):
    env = unit_env
    env.app.config["META_WHATSAPP_OUTBOUND_ENABLED"] = True
    env.app.static_folder = str(tmp_path)
    folder = tmp_path / "synthetic"
    folder.mkdir()
    # Incompressible synthetic PNG deliberately >100KB; a universal sticker-size
    # cap would reject a perfectly separate image path. This is no Meta max claim.
    data = random.Random(7).randbytes(512 * 512 * 4)
    Image.frombytes("RGBA", (512, 512), data).save(folder / "receipt.png")
    Image.new("RGBA", (512, 512), (20, 150, 80, 255)).save(folder / "sticker.webp", format="WEBP")
    records = {}
    for role, filename, mime, media_id in (
        ("receipt_header", "receipt.png", "image/png", "400001"),
        ("welcome_sticker", "sticker.webp", "image/webp", "400002"),
    ):
        raw = (folder / filename).read_bytes()
        records[role] = dict(contract_version=CONTRACT, tenant_id=env.tenant.id,
            connection_id=env.connection.id, sender_id=env.sender.id, app_id=env.event.sender.app_id,
            waba_id=env.event.sender.waba_id, phone_number_id=env.event.sender.phone_number_id,
            role=role, revision=1, media_id=media_id, static_path="synthetic/" + filename,
            configured_url="https://assets.example.test/static/synthetic/" + filename,
            sha256=hashlib.sha256(raw).hexdigest(), mime_type=mime, byte_length=len(raw),
            width=512, height=512, animated=False, frames=1, verified_at=env.now - 1,
            valid_until=env.now + 1800, verification_receipt_sha256="b" * 64)
    env.connection.config = {**env.connection.config, CATALOG_KEY: copy.deepcopy(records)}
    # Synthetic municipal tenant for the real scoped persisted-ticket producer.
    owner_id = env.owner.id
    env.tenant.pyme_id, env.tenant.municipio_id, env.tenant.tipo = None, owner_id, "municipio"
    env.owner.tipo_chat = "municipio"
    env.header_url = "https://assets.example.test/static/synthetic/receipt.png"
    env.sticker_url = "https://assets.example.test/static/synthetic/sticker.webp"
    env.tenant.configuracion = {"whatsapp_receipt_header": {"enabled": True, "image_url": env.header_url}}
    env.app.config["APP_BASE_URL"] = "https://www.chatboc.ar/chat"
    # This is a synthetic upload-receipt reader, NOT real Meta verification.
    env.app.extensions[UPLOAD_PROOF_VERIFIER] = lambda ref, identity, **_kwargs: VerifiedMediaUpload(
        identity, ref.media_id, ref.sha256, ref.verification_receipt_sha256, env.now - 1, env.now + 1800,
        configured_url=ref.configured_url)
    db.session.commit()
    _begin(env)
    env.records, env.asset_folder = records, folder
    return env


def _begin(env):
    if not env.actual_session.connection().connection.driver_connection.in_transaction:
        env.actual_session.execute(text("BEGIN"))


def _reference(env, role):
    return load_verified_media_reference(env.event.sender, role, session=db.session(), now=env.runtime.now())


def _catalog(env, records):
    env.connection.config = {**env.connection.config, CATALOG_KEY: copy.deepcopy(records)}
    db.session.commit()
    _begin(env)


def test_welcome_sticker_menu_reach_real_worker_in_order_without_twilio(media_env):
    env = media_env
    with _unit(env) as unit:
        sink = MetaConversationSink(unit)
        sink.emit_welcome(greeting="Hola, bienvenida sintética", menu=MetaReplyContent("text", "1. Reclamos\n2. Información"),
                          configured_sticker_url=env.sticker_url)
        receipt = unit.commit_reply(sink.bundle(), runtime=env.runtime)
    assert receipt.committed is True
    rows = WhatsAppOutboundAttempt.query.order_by(WhatsAppOutboundAttempt.sequence_no).all()
    assert [row.message_kind for row in rows] == ["text", "media", "text"]
    assert rows[1].payload_json["message"]["type"] == "sticker"
    assert env.calls["post"] == []
    for _ in range(3):
        assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id).status == "accepted"
    sent = [item["json"] for item in env.calls["post"]]
    assert [item["type"] for item in sent] == ["text", "sticker", "text"]
    assert sent[1]["sticker"] == {"id": "400002"}
    assert "1. Reclamos\n2. Información" == sent[2]["text"]["body"]
    assert env.calls["twilio"] == 0


def test_persisted_claim_header_primary_tracking_pin_and_followup_are_not_truncated(media_env):
    env = media_env
    ticket = MunicipioTicket(tenant_id=env.tenant.id, municipio_id=env.owner.id,
        nro_ticket="219611", consulta_pin="123456", categoria="Arbolado",
        telefono_vecino="+" + env.event.contact, anon_id="+" + env.event.contact)
    db.session.add(ticket)
    db.session.commit()
    ticket_id = ticket.id
    _begin(env)
    with _unit(env) as unit:
        sink = MetaConversationSink(unit)
        sink.emit_claim_receipt(ticket_id=ticket_id, callback_requested=False)
        bundle = sink.bundle()
        assert [item.kind for item in bundle.messages] == ["image", "text", "text"]
        assert bundle.messages[0].media.byte_length > 100000
        assert "M-219611" in bundle.messages[1].body and "/tracking/claim/219611#pin=123456" in bundle.messages[1].body
        assert "123456" in bundle.messages[2].body and "foto, un audio o un comentario" in bundle.messages[2].body
        unit.commit_reply(bundle, runtime=env.runtime)
    first = WhatsAppOutboundAttempt.query.order_by(WhatsAppOutboundAttempt.sequence_no).first()
    assert first.message_kind == "media"
    assert first.payload_json["message"]["type"] == "image"
    assert first.payload_json["message"]["image"] == {"id": "400001"}
    assert "caption" not in first.payload_json["message"]["image"]
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id).status == "accepted"
    assert env.calls["post"][0]["json"]["type"] == "image" and env.calls["twilio"] == 0


@pytest.mark.parametrize("case", ("missing", "foreign-tenant", "foreign-phone", "revision", "expired", "bytes", "mime", "animated-flag", "dimensions", "path", "proof-missing", "proof-wrong-id"))
def test_unverified_or_malformed_media_never_stages_or_posts(media_env, case):
    env = media_env
    records = copy.deepcopy(env.records)
    row = records["welcome_sticker"]
    if case == "missing":
        records.pop("welcome_sticker")
    elif case == "foreign-tenant":
        row["tenant_id"] += 1
    elif case == "foreign-phone":
        row["phone_number_id"] = "999999"
    elif case == "revision":
        row["revision"] = True
    elif case == "expired":
        row["valid_until"] = env.now
    elif case == "bytes":
        row["sha256"] = "c" * 64
    elif case == "mime":
        row["mime_type"] = "image/png"
    elif case == "animated-flag":
        row["animated"] = True
    elif case == "dimensions":
        row["width"] = 511
    elif case == "path":
        row["static_path"] = "../synthetic/sticker.webp"
    elif case == "proof-missing":
        env.app.extensions.pop(UPLOAD_PROOF_VERIFIER)
    else:
        env.app.extensions[UPLOAD_PROOF_VERIFIER] = lambda ref, identity, **_kwargs: VerifiedMediaUpload(
            identity, "999999", ref.sha256, ref.verification_receipt_sha256, env.now - 1, env.now + 1800,
            configured_url=ref.configured_url)
    _catalog(env, records)
    with pytest.raises(ConversationUnitOfWorkError):
        with _unit(env) as unit:
            sink = MetaConversationSink(unit)
            sink.emit_welcome(greeting="Hola", menu=MetaReplyContent("text", "Menú"), configured_sticker_url=env.sticker_url)
    assert WhatsAppOutboundAttempt.query.count() == 0 and env.calls["post"] == [] and env.calls["twilio"] == 0


def test_catalog_revoked_after_commit_is_checked_again_before_post(media_env):
    env = media_env
    with _unit(env) as unit:
        sink = MetaConversationSink(unit)
        sink.emit_welcome(greeting="Hola", menu=MetaReplyContent("text", "Menú"), configured_sticker_url=env.sticker_url)
        unit.commit_reply(sink.bundle(), runtime=env.runtime)
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id).status == "accepted"
    _catalog(env, {})
    result = worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id)
    assert result.status == "dead"
    assert len(env.calls["post"]) == 1 and env.calls["twilio"] == 0


@pytest.mark.parametrize("case", ("foreign-tenant", "other-phone"))
def test_receipt_reloads_persisted_ticket_in_same_tenant_and_contact(media_env, case):
    env = media_env
    owner = User(name="Synthetic foreign owner", email="foreign-media@example.invalid",
                 rol="admin", tipo_chat="municipio", password_hash="synthetic-not-a-login")
    db.session.add(owner)
    db.session.flush()
    foreign = TenantProfile(slug="synthetic-foreign-media", nombre="Synthetic foreign", tipo="municipio", municipio_id=owner.id)
    db.session.add(foreign)
    db.session.flush()
    ticket = MunicipioTicket(tenant_id=foreign.id if case == "foreign-tenant" else env.tenant.id,
        municipio_id=owner.id if case == "foreign-tenant" else env.owner.id, nro_ticket="synthetic-foreign-ticket",
        consulta_pin="654321", telefono_vecino="+" + env.event.contact if case == "foreign-tenant" else "+5491100000299")
    db.session.add(ticket)
    db.session.commit()
    ticket_id = ticket.id
    _begin(env)
    with pytest.raises(ConversationUnitOfWorkError, match="meta_receipt_ticket_scope_invalid"):
        with _unit(env) as unit:
            MetaConversationSink(unit).emit_claim_receipt(ticket_id=ticket_id)
    assert WhatsAppOutboundAttempt.query.count() == 0 and env.calls["post"] == []


def test_empty_human_output_completes_without_fabricating_delivery(media_env):
    env = media_env
    with _unit(env) as unit:
        sink = MetaConversationSink(unit)
        assert sink.bundle().messages == ()
        result = unit.commit_reply(sink.bundle(), runtime=env.runtime)
    assert result.committed is True and result.receipt.outbound_attempt_ids == ()
    row = WhatsAppInboundTurn.query.filter_by(turn_id=env.claim.turn_id).one()
    assert row.result_json["delivery_verified"] is False and row.result_json["outbound_count"] == 0
    assert WhatsAppOutboundAttempt.query.count() == 0 and env.calls["post"] == []


def test_bad_later_bundle_item_causes_zero_outbox_and_retains_original_claim(media_env):
    env = media_env
    with pytest.raises(MetaRuntimeError, match="meta_outbound_media_unsupported"):
        with _unit(env) as unit:
            reference = _reference(env, "welcome_sticker")
            unit.commit_reply(MetaReplyBundle((MetaReplyContent("sticker", "", media=reference),
                                             MetaReplyContent("audio", ""))), runtime=env.runtime)
    assert WhatsAppOutboundAttempt.query.count() == 0 and env.calls["post"] == []
    assert WhatsAppInboundTurn.query.filter_by(turn_id=env.claim.turn_id).one().status == "processing"


def test_media_bundle_duplicate_preserves_same_attempts_without_resend(media_env):
    env = media_env
    with _unit(env) as unit:
        reference = _reference(env, "welcome_sticker")
        bundle = MetaReplyBundle((MetaReplyContent("sticker", "", media=reference), MetaReplyContent("text", "Menú")))
        first = unit.commit_reply(bundle, runtime=env.runtime).receipt
    duplicate = produce_meta_service_reply(env.event, bundle, runtime=env.runtime)
    assert duplicate.outcome == "duplicate" and duplicate.outbound_attempt_ids == first.outbound_attempt_ids
    assert WhatsAppInboundTurn.query.count() == 1 and WhatsAppOutboundAttempt.query.count() == 2
    for _ in range(2):
        assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id).status == "accepted"
    again = produce_meta_service_reply(env.event, bundle, runtime=env.runtime)
    assert again.outcome == "duplicate" and again.outbound_attempt_ids == first.outbound_attempt_ids
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id).status == "idle"
    assert len(env.calls["post"]) == 2 and env.calls["twilio"] == 0


@pytest.mark.parametrize("field", ("_twilio_pre_messages", "image_url", "audio_url", "_welcome_sticker_urls"))
def test_raw_provider_or_llm_media_cannot_disappear_into_text_fallback(media_env, field):
    env = media_env
    with pytest.raises(ConversationUnitOfWorkError):
        with _unit(env) as unit:
            sink = MetaConversationSink(unit)
            sink.emit_response({field: [{"content_sid": "HXsynthetic"}] if field == "_twilio_pre_messages" else "https://evil.example/file"},
                               {"type": "text", "text": {"body": "Must not replace missing media"}})
    assert WhatsAppOutboundAttempt.query.count() == 0 and env.calls["post"] == []


def test_real_bitstream_marks_animated_sticker_not_just_mime(media_env):
    env = media_env
    path = env.asset_folder / "animated.webp"
    a, b = Image.new("RGBA", (512, 512), "red"), Image.new("RGBA", (512, 512), "blue")
    a.save(path, format="WEBP", save_all=True, append_images=[b], duration=[100, 100], loop=0)
    raw = path.read_bytes()
    records = copy.deepcopy(env.records)
    env.sticker_url = "https://assets.example.test/static/synthetic/animated.webp"
    records["welcome_sticker"].update(static_path="synthetic/animated.webp",
        configured_url=env.sticker_url, animated=True, frames=2,
        byte_length=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    _catalog(env, records)
    reference = _reference(env, "welcome_sticker")
    assert reference.animated is True and reference.frames == 2 and reference.mime_type == "image/webp"


def test_media_decode_preserves_budget_exhaustion_before_post(media_env, monkeypatch):
    env = media_env
    from services import meta_whatsapp_media as media
    checks = []
    def exhausted():
        checks.append(True)
        if len(checks) == 3:
            raise OutboxExecutionBudgetExceeded("synthetic_frame_budget")
    monkeypatch.setattr(media, "require_outbox_time_remaining", exhausted)
    with pytest.raises(OutboxExecutionBudgetExceeded, match="synthetic_frame_budget"):
        _reference(env, "welcome_sticker")
    assert checks == [True, True, True] and env.calls["post"] == []
    assert WhatsAppOutboundAttempt.query.count() == 0


def test_worker_media_budget_before_post_remains_retryable(media_env, monkeypatch):
    env = media_env
    with _unit(env) as unit:
        reference = _reference(env, "welcome_sticker")
        unit.commit_reply(MetaReplyBundle((MetaReplyContent("sticker", "", media=reference),)), runtime=env.runtime)
    from services import meta_whatsapp_media as media
    def exhausted():
        raise OutboxExecutionBudgetExceeded("synthetic_pre_post_budget")
    monkeypatch.setattr(media, "require_outbox_time_remaining", exhausted)
    result = worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id)
    assert result.status == "retry_wait" and result.error_code == "meta_outbound_budget_exhausted"
    assert env.calls["post"] == [] and env.calls["twilio"] == 0


@pytest.mark.parametrize("kind", ("image", "sticker"))
def test_cloud_payload_rejects_hidden_url(media_env, kind):
    env = media_env
    payload = cloud.media_payload(recipient=env.event.contact, kind=kind, media_id="400001")
    payload[kind]["link"] = "https://evil.example/asset"
    with pytest.raises(cloud.MetaContractError, match="meta_message_payload_invalid"):
        cloud.send_once(binding_loader=lambda: env.runtime.load_binding(env.event.sender), payload=payload,
                        now=env.runtime.now(), policy_check=lambda *_: True,
                        post=lambda *_a, **_k: pytest.fail("POST must not run"))
    assert env.calls["post"] == []


def test_uncertain_media_ack_preserves_fifo_and_never_retries(media_env, monkeypatch):
    env = media_env
    with _unit(env) as unit:
        reference = _reference(env, "welcome_sticker")
        unit.commit_reply(MetaReplyBundle((MetaReplyContent("sticker", "", media=reference), MetaReplyContent("text", "Menú"))), runtime=env.runtime)
    calls = []
    def uncertain(*_args, **_kwargs):
        calls.append(True)
        raise TimeoutError("synthetic_post_timeout")
    monkeypatch.setitem(env.app.extensions, RUNTIME_EXTENSION, replace(env.runtime, post=uncertain))
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id).status == "send_uncertain"
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id).status == "idle"
    assert calls == [True] and env.calls["twilio"] == 0
