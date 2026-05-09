import types
from services.municipio_responder import (
    responder_municipio,
    CONTEXTO_MUNICIPIO,
    clear_municipio_cache,
)
from models import ChatSessionContext, MunicipioTicket
from app import db


def run_turn(
    message,
    state=None,
    preset_dp=None,
    location=None,
    owner_user=None,
    flow_state=None,
    contact_info=None,
    set_state=True,
    anon_id="anon",
):
    existing = ChatSessionContext.query.get("test_session")
    if existing:
        db.session.delete(existing)
        db.session.commit()
    ctx = ChatSessionContext(chat_session_id="test_session", anon_id=anon_id)
    ctx.context_data = {}
    muni = ctx.context_data.setdefault(CONTEXTO_MUNICIPIO, {})
    if contact_info:
        muni["contacto_usuario"] = contact_info
    if set_state:
        muni["estado_conversacion"] = state or "ESPERANDO_SELECCION_MENU_PRINCIPAL"
    elif state is not None:
        muni["estado_conversacion"] = state
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
    clear_municipio_cache()
    resp = responder_municipio(
        pregunta_original=payload if location else message,
        owner_user=owner_user,
        viewer_user=None,
        rubro_obj=owner_user.rubro,
        chat_db_context=ctx,
        anon_id=anon_id,
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
    assert "Reclamo por *Arreglo de calle*" in result.response["message_body"]


def test_tree_text_triggers_arbolado(owner_user):
    result = run_turn(
        "ramas y arbol partido en mitad de la cuadra", owner_user=owner_user
    )
    assert result.ctx["estado_conversacion"] == "EN_FLUJO_RECLAMO"
    flow = result.ctx["reclamo_flow_v2"]
    assert flow["datos_reclamo"]["categoria"] == "Arbolado"


def test_hueco_en_vereda_maps_to_arreglo(owner_user):
    result = run_turn("hay un hueco en la vereda", owner_user=owner_user)
    assert result.ctx["estado_conversacion"] == "EN_FLUJO_RECLAMO"
    flow = result.ctx["reclamo_flow_v2"]
    assert flow["datos_reclamo"]["categoria"] == "Arreglo de calle"


def test_emoji_shortcut_from_main_menu(owner_user):
    result = run_turn("\U0001F4A1", owner_user=owner_user)
    assert result.ctx["estado_conversacion"] == "EN_FLUJO_RECLAMO"
    flow = result.ctx["reclamo_flow_v2"]
    assert flow["datos_reclamo"]["categoria"] == "Luminaria"


def test_emoji_shortcut_without_initial_state(owner_user):
    result = run_turn("\U0001F4A1", owner_user=owner_user, set_state=False)
    assert result.ctx["estado_conversacion"] == "EN_FLUJO_RECLAMO"
    flow = result.ctx["reclamo_flow_v2"]
    assert flow["datos_reclamo"]["categoria"] == "Luminaria"


def test_variation_selector_emoji_shortcut(owner_user):
    result = run_turn("\U0001F6E3\uFE0F", owner_user=owner_user)
    assert result.ctx["estado_conversacion"] == "EN_FLUJO_RECLAMO"
    flow = result.ctx["reclamo_flow_v2"]
    assert flow["datos_reclamo"]["categoria"] == "Arreglo de calle"


def test_car_emoji_triggers_licencia(owner_user):
    result = run_turn("\U0001F697", owner_user=owner_user)
    assert "Licencia de Conducir" in result.response["message_body"]


def test_car_variation_emoji_triggers_licencia(owner_user):
    result = run_turn("\U0001F697\uFE0F", owner_user=owner_user)
    assert "Licencia de Conducir" in result.response["message_body"]


def test_phone_emoji_triggers_contactos(owner_user):
    result = run_turn("\U0001F4DE", owner_user=owner_user)
    assert "Seleccioná una categoría" in result.response["message_body"]


def test_calendar_emoji_triggers_turnos(owner_user):
    result = run_turn("\U0001F4C5", owner_user=owner_user)
    assert any("Solicitar Turno" in opt.get("texto", "") for opt in result.response.get("options_list", []))


def test_tap_emoji_triggers_water(owner_user):
    result = run_turn("\U0001F6B0", owner_user=owner_user)
    flow = result.ctx["reclamo_flow_v2"]
    assert flow["datos_reclamo"]["categoria"] == "Pérdida de agua"


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


def test_reclamo_menu_action_with_explicit_action(owner_user):
    existing = ChatSessionContext.query.get("test_session")
    if existing:
        db.session.delete(existing)
        db.session.commit()

    ctx = ChatSessionContext(chat_session_id="test_session", anon_id="anon")
    ctx.context_data = {
        CONTEXTO_MUNICIPIO: {
            "estado_conversacion": "ESPERANDO_SELECCION_MENU_RECLAMOS",
        }
    }
    db.session.add(ctx)
    db.session.commit()

    payload = {"pregunta": "🚮", "action": "reclamo_limpieza_riego"}
    clear_municipio_cache()
    response = responder_municipio(
        pregunta_original=payload,
        owner_user=owner_user,
        viewer_user=None,
        rubro_obj=owner_user.rubro,
        chat_db_context=ctx,
        anon_id="anon",
        channel="whatsapp",
    )
    db.session.commit()

    updated_ctx = ChatSessionContext.query.get("test_session").context_data[CONTEXTO_MUNICIPIO]
    assert updated_ctx["estado_conversacion"] == "EN_FLUJO_RECLAMO"
    flow = updated_ctx["reclamo_flow_v2"]
    assert flow["datos_reclamo"]["categoria"] == "Limpieza y riego"
    assert "Limpieza y riego" in response.get("message_body", "")

    ctx_to_delete = ChatSessionContext.query.get("test_session")
    if ctx_to_delete:
        db.session.delete(ctx_to_delete)
        db.session.commit()


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


def test_llm_fallback_supplies_missing_fields(monkeypatch, owner_user):
    def fake_llm(text):
        return {
            "tipo_problema": "Arbolado",
            "descripcion_problema": "ramas caidas",
            "ubicacion_problema": "Don Bosco 55",
        }

    monkeypatch.setattr(
        "services.municipio_responder.extract_complaint_details_llm",
        fake_llm,
    )

    result = run_turn(
        "texto completamente desconocido",
        state="EN_FLUJO_RECLAMO",
        flow_state="ESPERANDO_CATEGORIA",
        owner_user=owner_user,
    )
    flow = result.ctx["reclamo_flow_v2"]
    assert flow["datos_reclamo"]["categoria"] == "Arbolado"
    assert flow["datos_reclamo"]["direccion"] == "Don Bosco 55"
    assert flow["datos_reclamo"]["descripcion"] == "ramas caidas"


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


def test_prefill_contact_from_previous_ticket(owner_user):
    ticket = MunicipioTicket(
        pregunta="Reporte anterior",
        categoria="Arbolado",
        municipio_id=owner_user.id,
        anon_id="anon",
        telefono_vecino="+5492611234567",
        email_vecino="marcelo@example.com",
        dni_vecino="32877851",
        nombre_vecino="Marcelo",
        nro_ticket="987654321_prefill",
        consulta_pin="123456",
    )
    db.session.add(ticket)
    db.session.commit()

    try:
        result = run_turn("hay una rama peligrosa", owner_user=owner_user)
        flow = result.ctx["reclamo_flow_v2"]
        datos = flow["datos_reclamo"]
        assert datos["email"] == "marcelo@example.com"
        assert datos["dni"] == "32877851"
        assert datos["telefono"] == "+5492611234567"
        assert datos["nombre"] == "Marcelo"
        assert "Para continuar, por favor, indicame la dirección exacta del problema" in result.response["message_body"]
        assert flow['state'] == 'ESPERANDO_DIRECCION'
    finally:
        db.session.delete(ticket)
        db.session.commit()


def test_prefill_contact_from_ticket_phone_match(owner_user):
    stored_phone = "+5492617778888"
    ticket = MunicipioTicket(
        pregunta="Ticket con telefono",
        categoria="Arbolado",
        municipio_id=owner_user.id,
        anon_id="prev_anon",
        telefono_vecino=stored_phone,
        email_vecino="contacto-previo@example.com",
        dni_vecino="30111222",
        nombre_vecino="Marcelo Contacto",
        nro_ticket="123123123_phone",
        consulta_pin="456789",
    )
    db.session.add(ticket)
    db.session.commit()

    try:
        result = run_turn(
            "se corto el arbol",
            owner_user=owner_user,
            anon_id="whatsapp_4_+5492617778888",
        )
        datos = result.ctx["reclamo_flow_v2"]["datos_reclamo"]
        assert datos["telefono"] == stored_phone
        assert datos["email"] == "contacto-previo@example.com"
        assert datos["dni"] == "30111222"
        assert datos["nombre"] == "Marcelo Contacto"
    finally:
        db.session.delete(ticket)
        db.session.commit()


def test_prefill_contact_from_previous_session(owner_user):
    anon = "+5492615550000"
    previous_ctx = ChatSessionContext(
        chat_session_id="previous_session",
        anon_id=anon,
        context_data={
            CONTEXTO_MUNICIPIO: {
                "contacto_usuario": {
                    "nombre": "Marcelo",
                    "dni": "30111222",
                    "email": "marcelo@example.com",
                    "telefono": anon,
                }
            }
        },
    )
    db.session.add(previous_ctx)
    db.session.commit()

    try:
        result = run_turn(
            "hay una rama peligrosa",
            owner_user=owner_user,
            anon_id=anon,
        )
        flow = result.ctx["reclamo_flow_v2"]
        datos = flow["datos_reclamo"]
        assert datos["email"] == "marcelo@example.com"
        assert datos["dni"] == "30111222"
        assert datos["telefono"] == anon
        assert datos["nombre"] == "Marcelo"
        assert flow["state"] == "ESPERANDO_DIRECCION"
    finally:
        db.session.delete(previous_ctx)
        db.session.commit()


def test_handle_direccion_uses_normalizer(monkeypatch, owner_user):
    def fake_norm(addr, cfg):
        return {
            "lat": -33.1,
            "lon": -68.6,
            "formatted": "Don Bosco 55",
            "maps_search_url": "http://maps" ,
        }

    monkeypatch.setattr(
        "services.address_normalizer.normalize_and_geocode", fake_norm
    )

    result = run_turn(
        "Don Bosco 55",
        state="EN_FLUJO_RECLAMO",
        flow_state="ESPERANDO_DIRECCION",
        owner_user=owner_user,
    )

    flow = result.ctx["reclamo_flow_v2"]
    assert flow["datos_reclamo"]["coordenadas"] == {"lat": -33.1, "lng": -68.6}
    assert "¿Es acá?" in result.response["message_body"]


def test_handle_direccion_without_geocode_does_not_ask_barrio(monkeypatch, owner_user):
    """If the address can't be geocoded, the flow should still proceed without
    asking the user for a barrio/distrito."""

    def fake_norm(addr, cfg):
        return {"formatted": "Don Bosco 55", "lat": None, "lon": None}

    monkeypatch.setattr(
        "services.address_normalizer.normalize_and_geocode", fake_norm
    )

    result = run_turn(
        "Don Bosco 55",
        state="EN_FLUJO_RECLAMO",
        flow_state="ESPERANDO_DIRECCION",
        owner_user=owner_user,
    )

    assert "barrio" not in result.response["message_body"].lower()


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

    import services.municipio_responder as municipio_responder

    monkeypatch.setattr(municipio_responder.servicio_tickets, "crear_nuevo_ticket", fake_crear_nuevo_ticket)

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
