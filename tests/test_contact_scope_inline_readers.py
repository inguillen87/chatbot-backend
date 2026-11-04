"""PREP: real inline consumers with disposable ORM identity and synthetic engines.

The lookup/parser, owner/tenant/session resolution, greeting handlers and menus are
real. Only local configuration and model/classifier boundaries are synthetic.
These cases do not establish authentication, provider delivery or production use.
"""
from copy import deepcopy
import sys

import pytest
from sqlalchemy.orm.attributes import flag_modified

from tests.contact_scope_fixtures import contact_env
from models import CategoriaTicket, MunicipioTicket, db
from services.contact_service import current_contact_phone, resolve_contact


CASES = (
    "channel", "actor", "wrong-phone", "ambiguous-phone", "renamed", "revoked", "explicit",
)
QUESTION = "Consulta breve"
EXPLICIT_NAME = "Explicit synthetic B"


def _trace_inline_reads(monkeypatch, consumer, responder):
    """Observe the actual inline calls, without replacing their DB validation."""
    original = consumer.read_contact_projection
    observed = []

    def read(value, *, tenant_id, phone):
        result = original(value, tenant_id=tenant_id, phone=phone)
        if sys._getframe(1).f_code is responder.__code__:
            observed.append({
                "tenant_id": tenant_id, "phone": phone, "value": deepcopy(value),
                "result": deepcopy(result),
            })
        return result

    monkeypatch.setattr(consumer, "read_contact_projection", read)
    return observed


def _identity(e, case):
    viewer, anon, expected_phone = None, e.phone, current_contact_phone(e.phone)
    if case == "actor":
        viewer, anon = e.b.user, None
        e.session.anon_id = None
        e.session.user_id = viewer.id
    elif case == "wrong-phone":
        anon = e.other_phone
        e.session.anon_id = e.other_phone
        expected_phone = current_contact_phone(e.other_phone)
    elif case == "ambiguous-phone":
        viewer = e.b.user
        viewer.telefono = e.other_phone
        expected_phone = None
    db.session.commit()
    return viewer, anon, expected_phone


def _seed(e, projection, *, explicit=False):
    e.session.context_data = e.cache_context(projection, explicit=explicit)
    db.session.commit()


def _restore_only_stale_convenience(e, projection):
    """Keep the same session and its actual flat planes from the first turn."""
    data = deepcopy(e.session.context_data)
    data["resolved_contact"] = deepcopy(projection)
    data["contact_cache"] = deepcopy(projection)
    e.session.context_data = data
    db.session.commit()


def _flat_names(data):
    pyme = data.get("contexto_pyme_v2") or {}
    municipal = data.get("contexto_municipio_v2") or {}
    contact = municipal.get("contacto_usuario") or {}
    user = municipal.get("user") or {}
    return (
        data.get("profile_name"), data.get("nombre_cliente"),
        pyme.get("nombre_cliente"), contact.get("nombre"), user.get("nombre"),
        user.get("name"),
    )


def _assert_inline_scope(reads, e, expected_phone, projection, *, accepted):
    assert len(reads) == 2  # Both direct reads in this actual responder were reached.
    assert all(row["tenant_id"] == e.b.tenant.id for row in reads)
    assert all(row["phone"] == expected_phone for row in reads)
    marked = [row for row in reads if row["value"] == projection]
    assert marked
    if accepted:
        assert all(row["result"] == projection for row in marked)
    else:
        assert all(row["result"] == {} for row in reads)


def _change_record(e, item, case):
    if case == "renamed":
        item.name = "Synthetic renamed canonical B"
    else:
        assert case == "revoked"
        item.tenant_id = e.a.tenant.id
    db.session.commit()


@pytest.mark.parametrize("case", CASES)
def test_real_pyme_inline_identity_is_current_and_never_promoted(contact_env, monkeypatch, case):
    from services import pymes

    e = contact_env
    # This case uses an actual PyME owner/profile, not a fabricated resolver result.
    e.b.tenant.tipo, e.b.tenant.pyme_id, e.b.tenant.municipio_id = "pyme", e.b.owner.id, None
    e.b.owner.tipo_chat, e.b.owner.pyme_id, e.b.owner.municipio_id = "pyme", e.b.owner.id, None
    e.b.user.tipo_chat, e.b.user.pyme_id, e.b.user.municipio_id = "pyme", e.b.owner.id, None
    db.session.commit()
    item = e.add_contact("b")
    original_name = item.name
    projection = resolve_contact(e.phone, None, tenant=e.b.tenant)
    assert projection["nombre"] == original_name
    assert projection["email"] == item.email != e.a.user.email
    _seed(e, projection, explicit=case == "explicit")
    explicit_draft = deepcopy(e.session.context_data.get("contexto_municipio_v2"))
    viewer, anon, expected_phone = _identity(e, case)
    reads = _trace_inline_reads(monkeypatch, pymes, pymes.responder_pyme)
    presented = []

    def model(**kwargs):
        # Public PyME menus are generic; observe the real name sent to its model
        # boundary, then let the actual SaludoHandler/menu finish this turn.
        presented.append(deepcopy(kwargs["usuario"]))
        return ({"accion_backend": "saludar", "datos_estructura": {"target": "pyme"},
                 "message_body": "", "botones": []}, None)

    monkeypatch.setattr(pymes, "cargar_configuracion_pyme", lambda *args, **kwargs: {})
    monkeypatch.setattr(pymes, "llamar_llm_con_fallback", model)
    # Optional product suggestions are an unrelated external model/catalog seam.
    monkeypatch.setattr(pymes, "sugerir_productos_relacionados", lambda *args, **kwargs: None)

    def invoke():
        with e.app.test_request_context("/synthetic-contact-inline", method="POST"):
            response = pymes.responder_pyme(
                QUESTION, e.b.owner, e.rubro, viewer_user=viewer,
                chat_db_context=e.session, anon_id=anon, channel="web",
                tenant_id=e.b.tenant.id, chat_session_uuid=e.session.chat_session_id,
            )
        # The anonymous responder already committed and expired this attribute.
        # Observe the actual persisted result; never force a product write here.
        db.session.expire(e.session, ["context_data"])
        assert isinstance(e.session.context_data, dict)
        assert isinstance(response, dict) and response.get("message_body")
        assert isinstance(response.get("options_list"), list)
        assert len(presented) == 1
        return presented[0]["nombre"]

    name = invoke()
    invalid = case in {"wrong-phone", "ambiguous-phone"}
    _assert_inline_scope(reads, e, expected_phone, projection, accepted=not invalid)
    if case == "explicit":
        assert name == EXPLICIT_NAME
        assert e.session.context_data["profile_name"] == EXPLICIT_NAME
        assert e.session.context_data["contexto_municipio_v2"] == explicit_draft
    elif invalid:
        assert name != original_name
    else:
        assert name == original_name
    assert original_name not in _flat_names(e.session.context_data)

    if case in {"renamed", "revoked"}:
        _change_record(e, item, case)
        _restore_only_stale_convenience(e, projection)
        reads.clear()
        presented.clear()
        assert invoke() != original_name
        _assert_inline_scope(reads, e, expected_phone, projection, accepted=False)
        assert original_name not in _flat_names(e.session.context_data)


@pytest.mark.parametrize("explicit", [False, True])
def test_real_municipal_cancel_keeps_draft_prefill_without_laundering_lookup(contact_env, monkeypatch, explicit):
    from services import municipio_responder

    e = contact_env
    item = e.add_contact("b")
    original_name = item.name
    projection = resolve_contact(e.phone, None, tenant=e.b.tenant)
    _seed(e, projection, explicit=explicit)
    if explicit:
        # An explicit declaration is allowed to be reused. Keep its channel and
        # draft names equal here so this case has one clear presentation oracle.
        data = deepcopy(e.session.context_data)
        data["contexto_municipio_v2"]["contacto_usuario"]["nombre"] = EXPLICIT_NAME
        e.session.context_data = data
    db.session.add(CategoriaTicket(tenant_id=e.b.tenant.id, tipo="ticket", nombre="Arbolado"))
    db.session.commit()
    before_tickets = MunicipioTicket.query.count()
    expected_name = EXPLICIT_NAME if explicit else original_name
    monkeypatch.setattr(municipio_responder, "cargar_configuracion_municipio", lambda *args, **kwargs: {})
    monkeypatch.setattr(municipio_responder.intent_classifier, "classify", lambda question: ("saludar", {}))
    monkeypatch.setattr(municipio_responder, "MUNICIPIO_RESPONSE_CACHE", {})
    reads = _trace_inline_reads(monkeypatch, municipio_responder, municipio_responder.responder_municipio)

    def invoke(payload):
        with e.app.test_request_context("/synthetic-contact-inline", method="POST"):
            response = municipio_responder.responder_municipio(
                payload, e.b.owner, e.rubro, viewer_user=None,
                chat_db_context=e.session, anon_id=e.phone, channel="web",
                tenant_profile=e.b.tenant, tenant_id=e.b.tenant.id,
                chat_session_uuid=e.session.chat_session_id,
            )
        flag_modified(e.session, "context_data")
        db.session.commit()
        assert isinstance(response, dict) and response.get("message_body")
        return response

    # Real direct action, real current tenant category and real start_flow. No
    # confirmation is supplied, and the caller supplies no invented contact data.
    invoke({"pregunta": QUESTION, "action_id": "reclamo_arbolado"})
    _assert_inline_scope(reads, e, current_contact_phone(e.phone), projection, accepted=True)
    municipal = e.session.context_data["contexto_municipio_v2"]
    draft = municipal["reclamo_flow_v2"]["datos_reclamo"]
    assert draft["categoria"] == "Arbolado"
    assert draft["nombre"] == expected_name  # Useful current prefill stays in the draft.
    assert original_name not in _flat_names(e.session.context_data)
    if explicit:
        assert municipal["contacto_usuario"]["nombre"] == EXPLICIT_NAME

    cancelled = invoke({"pregunta": "cancelar", "action_id": "reclamo_cancelar"})
    assert "cancelado" in cancelled["message_body"].lower()
    assert not e.session.context_data["contexto_municipio_v2"].get("reclamo_flow_v2")
    assert original_name not in _flat_names(e.session.context_data)
    if explicit:
        assert e.session.context_data["profile_name"] == EXPLICIT_NAME
        assert e.session.context_data["contexto_municipio_v2"]["contacto_usuario"]["nombre"] == EXPLICIT_NAME

    item.tenant_id = e.a.tenant.id
    db.session.commit()
    # The stale marked data is deliberately retained/reinserted as convenience
    # data. It never grants authority and must be rejected by the current DB read.
    _restore_only_stale_convenience(e, projection)
    reads.clear()
    response = invoke(QUESTION)
    _assert_inline_scope(reads, e, current_contact_phone(e.phone), projection, accepted=False)
    assert original_name not in response["message_body"]
    assert original_name not in _flat_names(e.session.context_data)
    if explicit:
        assert EXPLICIT_NAME in response["message_body"]
        assert e.session.context_data["profile_name"] == EXPLICIT_NAME
        assert e.session.context_data["contexto_municipio_v2"]["contacto_usuario"]["nombre"] == EXPLICIT_NAME
    assert MunicipioTicket.query.count() == before_tickets


@pytest.mark.parametrize("case", CASES)
def test_real_municipal_inline_identity_is_current_and_never_promoted(contact_env, monkeypatch, case):
    from services import municipio_responder

    e = contact_env
    item = e.add_contact("b")
    original_name = item.name
    projection = resolve_contact(e.phone, None, tenant=e.b.tenant)
    assert projection["nombre"] == original_name
    assert projection["email"] == item.email != e.a.user.email
    _seed(e, projection, explicit=case == "explicit")
    explicit_draft = deepcopy(e.session.context_data.get("contexto_municipio_v2"))
    viewer, anon, expected_phone = _identity(e, case)
    reads = _trace_inline_reads(monkeypatch, municipio_responder, municipio_responder.responder_municipio)
    classifier_calls = []

    def classify(question):
        classifier_calls.append(question)
        return "saludar", {}

    monkeypatch.setattr(municipio_responder, "cargar_configuracion_municipio", lambda *args, **kwargs: {})
    monkeypatch.setattr(municipio_responder.intent_classifier, "classify", classify)
    # A private per-case cache avoids any prior test's presentation replacing this
    # actual handler; the normal cache implementation is not under test here.
    monkeypatch.setattr(municipio_responder, "MUNICIPIO_RESPONSE_CACHE", {})

    def invoke():
        with e.app.test_request_context("/synthetic-contact-inline", method="POST"):
            response = municipio_responder.responder_municipio(
                QUESTION, e.b.owner, e.rubro, viewer_user=viewer,
                chat_db_context=e.session, anon_id=anon, channel="web",
                tenant_profile=e.b.tenant, tenant_id=e.b.tenant.id,
                chat_session_uuid=e.session.chat_session_id,
            )
        flag_modified(e.session, "context_data")
        db.session.commit()
        assert isinstance(response, dict) and response.get("message_body")
        assert classifier_calls == [QUESTION]
        return response["message_body"]

    body = invoke()
    invalid = case in {"wrong-phone", "ambiguous-phone"}
    _assert_inline_scope(reads, e, expected_phone, projection, accepted=not invalid)
    if case == "explicit":
        assert EXPLICIT_NAME in body and original_name not in body
        assert e.session.context_data["profile_name"] == EXPLICIT_NAME
        assert (e.session.context_data["contexto_municipio_v2"]["contacto_usuario"]
                == explicit_draft["contacto_usuario"])
    elif invalid:
        assert original_name not in body
    else:
        assert original_name in body
    assert original_name not in _flat_names(e.session.context_data)

    if case in {"renamed", "revoked"}:
        _change_record(e, item, case)
        _restore_only_stale_convenience(e, projection)
        reads.clear()
        classifier_calls.clear()
        assert original_name not in invoke()
        _assert_inline_scope(reads, e, expected_phone, projection, accepted=False)
        assert original_name not in _flat_names(e.session.context_data)
