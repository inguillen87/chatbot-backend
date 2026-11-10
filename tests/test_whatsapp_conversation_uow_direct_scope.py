"""V3 direct-API regression only; V1 22 and V2 14 cases stay byteexact.

Real ORM/Unit/finalizer entrypoints; synthetic identity and in-memory fixtures.
No application routes, providers or worker activation are installed.
"""
import pytest
from sqlalchemy import event

from models import WhatsAppInboundTurn, WhatsAppOutboundAttempt, db
from models_memory import Contact, ContactSnapshot, InteractionEvent
from services import meta_whatsapp_outbox as outbox
from services import whatsapp_conversation_turn as common
from services.meta_whatsapp_outbox import MetaReplyBundle
from services.whatsapp_conversation_uow import ConversationUnitOfWorkError
from tests.test_whatsapp_conversation_uow import environment, unit_env, _unit
from tests.test_whatsapp_conversation_uow_scope import scope_env


@pytest.mark.parametrize("entrypoint", ("flush", "commit_reply"))
def test_direct_unit_api_rejects_dirty_owner_pair_before_dml(scope_env, monkeypatch, entrypoint):
    env = scope_env
    before = (env.tenant.pyme_id, env.context.user_id, env.context.context_data.copy())
    calls = {"engine": 0, "crm": 0, "finalizer": 0}
    def observe_entry(original, key):
        def wrapped(*args, **kwargs):
            calls[key] += 1
            return original(*args, **kwargs)
        return wrapped
    monkeypatch.setattr(common, "finish_conversation_turn",
        observe_entry(common.finish_conversation_turn, "engine"))
    monkeypatch.setattr(common, "resolve_or_create_contact",
        observe_entry(common.resolve_or_create_contact, "crm"))
    monkeypatch.setattr(outbox, "complete_meta_service_reply",
        observe_entry(outbox.complete_meta_service_reply, "finalizer"))
    connection = env.actual_session.connection()
    dml = []
    def observe_sql(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            dml.append(statement.split(None, 1)[0].upper())
    event.listen(connection, "before_cursor_execute", observe_sql)
    try:
        with pytest.raises(ConversationUnitOfWorkError, match="conversation_uow_orm_scope_changed"):
            with _unit(env) as unit:
                # Mutations happen AFTER Unit entry. Both new IDs are mutually
                # consistent, so validating only persisted rows is insufficient.
                env.tenant.pyme_id = env.other_owner.id
                env.context.user_id = env.other_owner.id
                env.context.context_data = {**before[2], "synthetic_allowed_context_wip": True}
                if entrypoint == "flush":
                    unit.flush()
                else:
                    unit.commit_reply(MetaReplyBundle(()), runtime=env.runtime)
    finally:
        event.remove(connection, "before_cursor_execute", observe_sql)
    assert calls == {"engine": 0, "crm": 0, "finalizer": 0} and dml == []
    assert (env.tenant.pyme_id, env.context.user_id, env.context.context_data) == before
    assert Contact.query.count() == ContactSnapshot.query.count() == InteractionEvent.query.count() == 0
    assert WhatsAppOutboundAttempt.query.count() == 0
    assert WhatsAppInboundTurn.query.filter_by(turn_id=env.claim.turn_id).one().status == "processing"
    assert env.calls["post"] == [] and env.calls["twilio"] == 0
