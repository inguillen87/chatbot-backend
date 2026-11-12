"""Combined canary: native welcome with production-shaped isolated Meta policy."""
import pytest

from models import ChannelSessionIdentityBinding, ChatSessionContext, db
from services import whatsapp_inbound_worker as worker
from tests.test_meta_provider_isolation import _isolate, _legacy_config, _resolve, _snapshot
from tests.test_meta_whatsapp_inbound_worker import _post, _turn
from tests.test_meta_whatsapp_outbox import CONTACT, environment
from tests.test_meta_whatsapp_welcome import _attempts, _bodies, _prepare, _process


@pytest.mark.parametrize("identity_mode", ("legacy", "shadow"))
def test_hola_isolated_meta_keeps_twilio_history_and_commits_exact_text_bundle(environment, identity_mode):
    env = environment
    _legacy_config(env)
    legacy = ChatSessionContext(chat_session_id="synthetic-welcome-twilio-history",
        tenant_id=env.tenant.id, user_id=env.owner.id, anon_id="+" + CONTACT,
        context_data={"history_marker": "preserve-existing-twilio"})
    db.session.add(legacy)
    db.session.commit()
    twilio = _resolve(env, "twilio")
    old_binding = _snapshot(db.session.get(ChannelSessionIdentityBinding, twilio.binding_id))
    old_context = _snapshot(db.session.get(ChatSessionContext, twilio.chat_session_id))
    db.session.commit()
    _isolate(env)
    env.app.config["CHANNEL_SESSION_IDENTITY_MODE"] = identity_mode

    tenant_id, turn_id = _prepare(env)
    assert _turn(turn_id).session_identity_version == "meta-v1"
    assert _turn(turn_id).chat_session_id != twilio.chat_session_id
    assert not _attempts(turn_id) and env.calls["post"] == []
    _process(tenant_id, turn_id)
    assert _bodies(turn_id) == [
        "¡Hola! Soy JUNI, asistente virtual de Junín.",
        "Podés usar estas opciones:\n\n• Escribí Iniciar reclamo.\n• Escribí Encuestas.",
    ]
    assert env.calls["post"] == [] and env.calls["twilio"] == 0
    assert _post(env, body="hola") == turn_id
    assert worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)["processed"] == 0
    assert len(_attempts(turn_id)) == 2
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=tenant_id).status == "accepted"
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=tenant_id).status == "accepted"
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=tenant_id).status == "idle"
    assert len(env.calls["post"]) == 2 and env.calls["twilio"] == 0
    db.session.expire_all()
    assert _snapshot(db.session.get(ChannelSessionIdentityBinding, twilio.binding_id)) == old_binding
    assert _snapshot(db.session.get(ChatSessionContext, twilio.chat_session_id)) == old_context
    assert env.app.config["WHATSAPP_INBOUND_DURABILITY_MODE"] == "legacy"
    assert env.app.config["CHANNEL_SESSION_IDENTITY_MODE"] == identity_mode
    assert "WHATSAPP_INBOUND_HASH_SECRET" not in env.app.config
    assert "CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1" not in env.app.config
