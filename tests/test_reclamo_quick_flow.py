import types
from services.municipio_responder import responder_municipio, CONTEXTO_MUNICIPIO
from models import ChatSessionContext
from app import db


def run_turn(message, state=None, preset_dp=None, location=None, owner_user=None, flow_state=None, contact_info=None):
    existing = ChatSessionContext.query.get("test_session")
    if existing:
        db.session.delete(existing)
        db.session.commit()
    ctx = ChatSessionContext(chat_session_id="test_session")
    ctx.context_data = {}
    muni = ctx.context_data.setdefault(CONTEXTO_MUNICIPIO, {})
    if contact_info:
        muni["contacto_usuario"] = contact_info
    muni["estado_conversacion"] = state or "ESPERANDO_SELECCION_MENU_PRINCIPAL"
    if flow_state:
        muni["reclamo_flow_v2"] = {"state": flow_state, "datos_reclamo": {}}
    if preset_dp:
        muni["datos_parciales_llm_reclamo"] = preset_dp
    db.session.add(ctx)
    db.session.commit()
    payload = {"pregunta": message}
    if location:
        payload["ubicacion_usuario"] = location
        payload["es_ubicacion"] = True
    resp = responder_municipio(
        pregunta_original=payload if location else message,
        owner_user=owner_user,
        viewer_user=None,
        rubro_obj=owner_user.rubro,
        chat_db_context=ctx,
        anon_id="anon",
        channel="whatsapp",
    )
    db.session.commit()
    ctx_after = ChatSessionContext.query.get("test_session").context_data[CONTEXTO_MUNICIPIO]
    return types.SimpleNamespace(response=resp, ctx=ctx_after)


def test_free_text_sets_category_and_asks_address(owner_user):
    result = run_turn("hay un agujero en mi cuadra", owner_user=owner_user)
    assert result.ctx["estado_conversacion"] == "EN_FLUJO_RECLAMO"
    flow = result.ctx["reclamo_flow_v2"]
    assert flow["datos_reclamo"]["categoria"] == "Arreglo de calle"


def test_tree_text_triggers_arbolado(owner_user):
    result = run_turn(
        "ramas y arbol partido en mitad de la cuadra", owner_user=owner_user
    )
    assert result.ctx["estado_conversacion"] == "EN_FLUJO_RECLAMO"
    flow = result.ctx["reclamo_flow_v2"]
    assert flow["datos_reclamo"]["categoria"] == "Arbolado"


def test_numeric_selection_maps_to_category(owner_user):
    result = run_turn(
        "5",
        state="EN_FLUJO_RECLAMO",
        flow_state="ESPERANDO_CATEGORIA",
        owner_user=owner_user,
    )
    flow = result.ctx["reclamo_flow_v2"]
    assert flow["datos_reclamo"]["categoria"] == "Arreglo de calle"


def test_emoji_selection_maps_to_category(owner_user):
    result = run_turn(
        "🌳",
        state="EN_FLUJO_RECLAMO",
        flow_state="ESPERANDO_CATEGORIA",
        owner_user=owner_user,
    )
    flow = result.ctx["reclamo_flow_v2"]
    assert flow["datos_reclamo"]["categoria"] == "Arbolado"


def test_free_text_in_category_state(owner_user):
    result = run_turn(
        "hay un agujero en mi cuadra",
        state="EN_FLUJO_RECLAMO",
        flow_state="ESPERANDO_CATEGORIA",
        owner_user=owner_user,
    )
    flow = result.ctx["reclamo_flow_v2"]
    assert flow["datos_reclamo"]["categoria"] == "Arreglo de calle"
    assert flow["datos_reclamo"]["descripcion"] == "hay un agujero en mi cuadra"


def test_prefill_dni_from_contact(owner_user):
    contact = {
        "dni": "32877851",
        "email": "vecino@example.com",
        "nombre": "Vecino",
        "telefono": "+5492610000000",
    }
    result = run_turn(
        "ramas caidas en la vereda",
        owner_user=owner_user,
        contact_info=contact,
    )
    flow = result.ctx["reclamo_flow_v2"]
    assert flow["datos_reclamo"]["dni"] == "32877851"


def test_pin_moves_to_personal_data(owner_user):
    location = {"latitude": -33.0, "longitude": -68.5, "address": "Calle Falsa 123"}
    result = run_turn("", state="ESPERANDO_DIRECCION_RECLAMO", location=location, owner_user=owner_user)
    assert result.ctx["estado_conversacion"] == "ESPERANDO_DATOS_PERSONALES"
    assert "ubicacion" in result.ctx["datos_parciales_llm_reclamo"]


def test_no_pedir_tipo_if_categoria_present(owner_user):
    preset = {"categoria": "Arbolado"}
    result = run_turn(
        "don bosco esquina sarmiento",
        state="ESPERANDO_DIRECCION_RECLAMO",
        preset_dp=preset,
        owner_user=owner_user,
    )
    assert result.ctx.get("estado_conversacion") != "ESPERANDO_INFO_RECLAMO_LLM"


def test_ticket_confirmation_uses_stored_context(monkeypatch, owner_user):
    captured = {}

    def fake_crear_nuevo_ticket(kind, data):
        captured.update(data)
        return {"nro_ticket": "T-1"}

    from services.ticket_service import servicio_tickets

    monkeypatch.setattr(servicio_tickets, "crear_nuevo_ticket", fake_crear_nuevo_ticket)

    preset = {
        "categoria": "Arbolado",
        "descripcion": "desc",
        "ubicacion": "Calle Falsa 123",
        "coordenadas": {"lat": -33.0, "lng": -68.5},
        "nombre": "Juan",
        "telefono": "+5492611111111",
        "dni": "12345678",
    }

    run_turn("confirmo", state="ESPERANDO_CONFIRMACION_RECLAMO", preset_dp=preset, owner_user=owner_user)

    assert captured["categoria"] == "Arbolado"
    assert captured["direccion"] == "Calle Falsa 123"
