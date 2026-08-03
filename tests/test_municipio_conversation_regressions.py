from copy import deepcopy
import json
from unittest.mock import MagicMock, patch

from models import TenantProfile, User, db
from services.actions.municipio_actions import CrearReclamoActionHandler
from services.constants import CONTEXTO_MUNICIPIO
from services.llm_utils import extract_complaint_details_llm
from services.municipio_responder import (
    ReclamoFlowHandler,
    ReclamoState,
    _maybe_route_menu_input_to_llm,
    _try_start_reclamo_from_text,
    extract_reclamo_details_from_text,
)


VOICE_CLAIM_WITH_CALLBACK = (
    "Tengo un poste caído acá en el barrio, acá en Palmira. "
    "¿Puedes ayudarme? ¿Puedes llamarme por teléfono o te llamo yo?"
)


def _complete_claim(**overrides):
    claim = {
        "categoria": "Luminaria",
        "descripcion": "Tengo un poste caído en el barrio.",
        "direccion": "Don Bosco 56, Palmira",
        "nombre": "Marcelo",
        "dni": "32877851",
        "email": "marcelo@example.com",
        "telefono": "+5492613168608",
    }
    claim.update(overrides)
    return claim


def _handler_for(state, claim):
    flow = {
        "state": state.name,
        "confirmation_id": "claim-regression-001",
        "datos_reclamo": claim,
    }
    context = {
        "channel": "whatsapp",
        "chat_db_context_data": {
            CONTEXTO_MUNICIPIO: {
                "estado_conversacion": "EN_FLUJO_RECLAMO",
                "reclamo_flow_v2": flow,
            }
        },
    }
    return ReclamoFlowHandler(context, chat_db_context=None)


def test_audio_claim_preserves_llm_callback_request_through_guided_draft():
    llm_result = {
        "intencion": "crear_reclamo",
        "es_reclamo": True,
        "tipo_problema": "Luminaria",
        "descripcion_problema": "Poste caído en el barrio de Palmira.",
        "solicita_llamada": True,
        "motivo_llamada": "Quiere coordinar el reclamo por teléfono.",
    }

    with (
        patch(
            "services.municipio_responder.extract_multiple_contact_details_llm",
            return_value={},
        ),
        patch(
            "services.municipio_responder.extract_complaint_details_llm",
            return_value=llm_result,
        ) as complaint_llm,
        patch(
            "services.municipio_ai_classifier.infer_reclamo_category",
            return_value=None,
        ),
        patch(
            "services.municipio_ai_classifier.infer_reclamo_priority",
            return_value=None,
        ),
        patch(
            "services.municipio_ai_classifier.infer_reclamo_operational_signals",
            return_value=None,
        ),
        patch(
            "services.municipio_ai_classifier.infer_reclamo_sentiment",
            return_value=None,
        ),
    ):
        details = extract_reclamo_details_from_text(
            VOICE_CLAIM_WITH_CALLBACK,
            [{"texto": "Luminaria"}],
            default_localidad="Palmira",
            default_provincia="Mendoza",
        )

    complaint_llm.assert_called_once()
    assert details["solicita_llamada"] is True
    assert details["motivo_llamada"] == "Quiere coordinar el reclamo por teléfono."

    context = {
        "channel": "whatsapp",
        "chat_db_context_data": {
            CONTEXTO_MUNICIPIO: {
                "contacto_usuario": {
                    "nombre": "Marcelo",
                    "dni": "32877851",
                    "email": "marcelo@example.com",
                    "telefono": "+5492613168608",
                }
            }
        },
    }
    with patch(
        "services.municipio_responder.extract_reclamo_details_from_text",
        return_value=details,
    ):
        response = _try_start_reclamo_from_text(
            VOICE_CLAIM_WITH_CALLBACK,
            context,
            chat_db_context=None,
            default_localidad="Palmira",
            default_provincia="Mendoza",
        )

    flow = context["chat_db_context_data"][CONTEXTO_MUNICIPIO]["reclamo_flow_v2"]
    assert response is not None
    assert flow["datos_reclamo"]["solicita_llamada"] is True
    assert (
        flow["datos_reclamo"]["motivo_llamada"]
        == "Quiere coordinar el reclamo por teléfono."
    )


def test_audio_claim_from_main_menu_uses_structured_bootstrap_and_keeps_callback():
    """A visible menu must not reduce a natural voice note to a keyword."""

    municipal_context = {
        "estado_conversacion": "ESPERANDO_SELECCION_MENU_PRINCIPAL",
        "contacto_usuario": {
            "nombre": "Marcelo",
            "dni": "32877851",
            "email": "marcelo@example.com",
            "telefono": "+5492613168608",
        },
    }
    context = {
        "channel": "whatsapp",
        "municipio_config_actual": {
            "ciudad": "Palmira",
            "provincia": "Mendoza",
        },
        "chat_db_context_data": {CONTEXTO_MUNICIPIO: municipal_context},
    }
    extracted = {
        "intencion": "crear_reclamo",
        "es_reclamo": True,
        "categoria": "Luminaria",
        "descripcion": "Poste caído en el barrio de Palmira.",
        "solicita_llamada": True,
        "motivo_llamada": "Quiere coordinar el reclamo por teléfono.",
    }

    with patch(
        "services.municipio_responder.extract_reclamo_details_from_text",
        return_value=extracted,
    ) as extractor:
        response = _maybe_route_menu_input_to_llm(
            VOICE_CLAIM_WITH_CALLBACK,
            municipal_context,
            app=None,
            context=context,
            viewer_user=None,
            owner_user=None,
            chat_db_context=None,
        )

    extractor.assert_called_once()
    assert extractor.call_args.kwargs == {
        "default_localidad": "Palmira",
        "default_provincia": "Mendoza",
        "require_claim_intent": True,
    }
    assert response is not None
    flow = municipal_context["reclamo_flow_v2"]
    assert municipal_context["estado_conversacion"] == "EN_FLUJO_RECLAMO"
    assert flow["datos_reclamo"]["descripcion"] == extracted["descripcion"]
    assert flow["datos_reclamo"]["solicita_llamada"] is True
    assert flow["datos_reclamo"]["motivo_llamada"] == extracted["motivo_llamada"]


def test_informational_tree_area_question_from_menu_does_not_start_claim():
    question = "¿Qué área mantiene el arbolado público?"
    municipal_context = {
        "estado_conversacion": "ESPERANDO_SELECCION_MENU_PRINCIPAL",
        "menu_opciones": [{"texto": "Reclamos"}],
    }
    context = {
        "channel": "whatsapp",
        "chat_db_context_data": {CONTEXTO_MUNICIPIO: municipal_context},
    }
    classified_details = {
        "intencion": "consulta_informativa",
        "es_reclamo": False,
        # A category is valid extraction metadata, not authorization to mutate
        # the conversation into a claim flow.
        "categoria": "Arbolado",
        "descripcion": "Consulta por el área responsable del arbolado público.",
    }
    informational_response = {
        "message_body": "El área responsable depende de la organización municipal.",
        "message_type": "text",
        "options_list": [],
    }

    with (
        patch(
            "services.municipio_responder.extract_reclamo_details_from_text",
            return_value=classified_details,
        ) as extractor,
        patch(
            "services.municipio_responder.handle_llm_interaction",
            return_value=(informational_response, municipal_context),
        ) as general_llm,
    ):
        response = _maybe_route_menu_input_to_llm(
            question,
            municipal_context,
            app=None,
            context=context,
            viewer_user=None,
            owner_user=None,
            chat_db_context=None,
        )

    extractor.assert_called_once()
    general_llm.assert_called_once()
    assert response == informational_response
    assert "reclamo_flow_v2" not in municipal_context
    assert (
        municipal_context["estado_conversacion"]
        == "CONVERSACION_GENERAL_LLM"
    )


def test_claim_intent_provider_failure_from_menu_fails_closed():
    message = "Hay un poste caído en Palmira, ¿pueden llamarme?"
    municipal_context = {
        "estado_conversacion": "ESPERANDO_SELECCION_MENU_PRINCIPAL",
    }
    context = {
        "channel": "whatsapp",
        "municipio_config_actual": {
            "ciudad": "Palmira",
            "provincia": "Mendoza",
        },
        "chat_db_context_data": {CONTEXTO_MUNICIPIO: municipal_context},
    }
    safe_fallback = {
        "message_body": "No pude interpretar la solicitud en este momento.",
        "message_type": "text",
        "options_list": [],
    }

    with (
        patch(
            "services.llm_utils.robust_chat",
            side_effect=RuntimeError("provider unavailable"),
        ),
        patch(
            "services.municipio_ai_classifier.infer_reclamo_category",
            return_value=None,
        ),
        patch(
            "services.municipio_ai_classifier.infer_reclamo_priority",
            return_value=None,
        ),
        patch(
            "services.municipio_ai_classifier.infer_reclamo_operational_signals",
            return_value=None,
        ),
        patch(
            "services.municipio_ai_classifier.infer_reclamo_sentiment",
            return_value=None,
        ),
        patch(
            "services.municipio_responder.handle_llm_interaction",
            return_value=(safe_fallback, municipal_context),
        ) as general_llm,
    ):
        response = _maybe_route_menu_input_to_llm(
            message,
            municipal_context,
            app=None,
            context=context,
            viewer_user=None,
            owner_user=None,
            chat_db_context=None,
        )

    general_llm.assert_called_once()
    assert response == safe_fallback
    assert "reclamo_flow_v2" not in municipal_context
    assert (
        municipal_context["estado_conversacion"]
        == "CONVERSACION_GENERAL_LLM"
    )


def test_callback_extraction_uses_llm_reason_but_requires_explicit_user_request():
    with patch(
        "services.llm_utils.robust_chat",
        return_value=json.dumps(
            {
                "tipo_problema": "Luminaria",
                "descripcion_problema": "Poste caído en Palmira.",
                "solicita_llamada": True,
                "motivo_llamada": "Coordinar atención por el poste caído.",
            }
        ),
    ):
        result = extract_complaint_details_llm(VOICE_CLAIM_WITH_CALLBACK)

    assert result["solicita_llamada"] is True
    assert result["motivo_llamada"] == "Coordinar atención por el poste caído."

    with patch(
        "services.llm_utils.robust_chat",
        return_value=json.dumps(
            {
                "descripcion_problema": "Consulta por teléfono municipal.",
                "solicita_llamada": True,
                "motivo_llamada": "Llamar al vecino.",
            }
        ),
    ):
        informational = extract_complaint_details_llm(
            "¿A qué número llamo yo para consultar el estado?"
        )

    assert "solicita_llamada" not in informational
    assert "motivo_llamada" not in informational


def test_complaint_intent_contract_fails_closed_on_incomplete_or_conflicting_output():
    unsafe_provider_payloads = (
        {
            "intencion": "crear_reclamo",
            "descripcion_problema": "Poste caído en Palmira.",
            # Missing the independent boolean confirmation.
        },
        {
            "intencion": "crear_reclamo",
            "es_reclamo": False,
            "descripcion_problema": "Poste caído en Palmira.",
        },
        {
            "intencion": "consulta_informativa",
            "es_reclamo": True,
            "descripcion_problema": "Consulta por alumbrado.",
        },
    )

    for provider_payload in unsafe_provider_payloads:
        with patch(
            "services.llm_utils.robust_chat",
            return_value=json.dumps(provider_payload),
        ):
            result = extract_complaint_details_llm(
                "Hay un poste caído en Palmira."
            )

        assert result["intencion"] == "ambiguo"
        assert result["es_reclamo"] is False


def test_nada_mas_in_photo_state_skips_photo_and_reaches_confirmation():
    handler = _handler_for(ReclamoState.ESPERANDO_FOTO, _complete_claim())

    response = handler.handle("Nada más.", {"pregunta": "Nada más."})

    assert handler.flow_context["state"] == ReclamoState.ESPERANDO_CONFIRMACION.name
    assert handler.flow_context["datos_reclamo"]["foto_url"] is None
    assert "confirm" in response["message_body"].lower()
    assert "no entend" not in response["message_body"].lower()


def test_vague_landmark_is_rejected_then_exact_address_and_photo_preserve_audio_claim():
    claim = _complete_claim(
        direccion=None,
        descripcion=VOICE_CLAIM_WITH_CALLBACK,
        solicita_llamada=True,
        motivo_llamada="Quiere coordinar el reclamo por teléfono.",
    )
    initial_claim = deepcopy(claim)
    handler = _handler_for(ReclamoState.ESPERANDO_DIRECCION, claim)
    vague_address_turn = "Justamente en la esquina de la plaza."
    exact_address_turn = "Don Bosco 56 esquina Sarmiento, Palmira"

    with patch(
        "services.address_normalizer.normalize_and_geocode",
        return_value=None,
    ) as normalize_address:
        vague_response = handler.handle(
            vague_address_turn,
            {"pregunta": vague_address_turn},
        )

        assert handler.flow_context["datos_reclamo"] == initial_claim
        assert handler.flow_context["state"] == ReclamoState.ESPERANDO_DIRECCION.name
        assert "direcci" in vague_response["message_body"].lower()
        normalize_address.assert_not_called()

        address_response = handler.handle(
            exact_address_turn,
            {"pregunta": exact_address_turn},
        )

    normalize_address.assert_called_once_with(exact_address_turn, {})
    expected_after_address = deepcopy(initial_claim)
    expected_after_address["direccion"] = exact_address_turn
    assert handler.flow_context["datos_reclamo"] == expected_after_address
    assert handler.flow_context["state"] == ReclamoState.ESPERANDO_FOTO.name
    assert "foto" in address_response["message_body"].lower()

    photo_url = "https://media.example/claim-evidence-001.jpg"
    photo_response = handler.handle(
        "",
        {
            "pregunta": "",
            "es_foto": True,
            "foto_url": photo_url,
            "archivo_id_para_asociar": 701,
        },
    )

    expected_at_confirmation = deepcopy(expected_after_address)
    expected_at_confirmation.update(
        {
            "foto_url": photo_url,
            "archivo_id_para_asociar": 701,
        }
    )
    assert handler.flow_context["datos_reclamo"] == expected_at_confirmation
    assert handler.flow_context["state"] == ReclamoState.ESPERANDO_CONFIRMACION.name
    assert "confirm" in photo_response["message_body"].lower()
    assert exact_address_turn in photo_response["message_body"]
    assert "llamada telefónica pendiente" in photo_response["message_body"]


def test_address_correction_at_confirmation_preserves_every_other_claim_field():
    claim = _complete_claim(
        solicita_llamada=True,
        motivo_llamada="Quiere coordinar el reclamo por teléfono.",
        foto_url="https://media.example/claim-evidence-001.jpg",
        archivo_id_para_asociar=701,
        coordenadas={"lat": -33.081, "lng": -68.469},
        map_search_url="https://maps.example/old-location",
        maps_link="https://maps.example/old-link",
        static_map_url="https://maps.example/old-preview.png",
    )
    initial_claim = deepcopy(claim)
    handler = _handler_for(ReclamoState.ESPERANDO_CONFIRMACION, claim)
    corrected_address = "Don Bosco 56 esquina Sarmiento, Plaza Junín"

    response = handler.handle(
        f"La dirección es {corrected_address}.",
        {"pregunta": f"La dirección es {corrected_address}."},
    )

    expected_claim = deepcopy(initial_claim)
    expected_claim["direccion"] = corrected_address
    for stale_location_field in (
        "coordenadas",
        "map_search_url",
        "maps_link",
        "static_map_url",
    ):
        expected_claim.pop(stale_location_field)

    assert handler.flow_context["datos_reclamo"] == expected_claim
    assert handler.flow_context["state"] == ReclamoState.ESPERANDO_CONFIRMACION.name
    assert corrected_address in response["message_body"]
    assert initial_claim["direccion"] not in response["message_body"]
    assert "Foto adjunta: Sí" in response["message_body"]
    assert "llamada telefónica pendiente" in response["message_body"]


def test_audio_address_correction_after_edit_updates_claim_not_contact_address():
    claim = _complete_claim(
        direccion="Justamente en la esquina de la plaza.",
        coordenadas={"lat": -33.081, "lon": -68.469},
    )
    handler = _handler_for(ReclamoState.ESPERANDO_CONFIRMACION, claim)

    edit_response = handler.handle(
        "Editar datos",
        {"action_id": "reclamo_confirmar_no"},
    )

    assert handler.flow_context["state"] == ReclamoState.ESPERANDO_DATOS_CONTACTO.name
    assert handler.flow_context["contact_edit_mode"] is True
    assert "corregir" in edit_response["message_body"].lower()

    transcript = (
        "La ubicación no estuvo bien guardada. La ubicación era Sarmiento, "
        "esquina San Martín, en la Plaza de Jujuy. "
        "¿Puedes modificar el reclamo y poner bien la dirección?"
    )
    response = handler.handle(
        transcript,
        {
            "pregunta": transcript,
            "es_audio": True,
            "es_archivo": True,
            "archivo_id_para_asociar": 472,
        },
    )

    assert handler.flow_context["state"] == ReclamoState.ESPERANDO_CONFIRMACION.name
    assert "contact_edit_mode" not in handler.flow_context
    assert handler.flow_context["datos_reclamo"]["direccion"] == (
        "Sarmiento, esquina San Martín, en la Plaza de Jujuy"
    )
    assert "direccion_contacto" not in handler.flow_context["datos_reclamo"]
    assert "coordenadas" not in handler.flow_context["datos_reclamo"]
    assert "Sarmiento" in response["message_body"]


def test_complete_legacy_contact_state_recovers_before_multimodal_correction():
    claim = _complete_claim(direccion="Dirección anterior")
    handler = _handler_for(ReclamoState.ESPERANDO_DATOS_CONTACTO, claim)
    transcript = (
        "Sí, quiero modificar la dirección. La dirección es Sarmiento y "
        "Salaberry, en la esquina."
    )

    response = handler.handle(
        transcript,
        {
            "pregunta": transcript,
            "es_audio": True,
            "whatsapp_inbound_content": {
                "contract_version": "whatsapp.inbound_content.v1",
                "kind": "audio",
            },
        },
    )

    assert handler.flow_context["state"] == ReclamoState.ESPERANDO_CONFIRMACION.name
    assert handler.flow_context["datos_reclamo"]["direccion"] == (
        "Sarmiento y Salaberry, en la esquina"
    )
    assert "direccion_contacto" not in handler.flow_context["datos_reclamo"]
    assert "Sarmiento y Salaberry" in response["message_body"]


def test_contact_edit_email_alias_does_not_overwrite_claim_address():
    claim = _complete_claim(
        direccion="Don Bosco 56 esquina Sarmiento",
        email="anterior@example.com",
    )
    handler = _handler_for(ReclamoState.ESPERANDO_CONFIRMACION, claim)
    handler.handle("Editar datos", {"action_id": "reclamo_confirmar_no"})

    response = handler.handle(
        "Mi email correcto es qa.ubicacion@example.com",
        {"pregunta": "Mi email correcto es qa.ubicacion@example.com"},
    )

    assert handler.flow_context["state"] == ReclamoState.ESPERANDO_CONFIRMACION.name
    assert handler.flow_context["datos_reclamo"]["email"] == (
        "qa.ubicacion@example.com"
    )
    assert handler.flow_context["datos_reclamo"]["direccion"] == (
        "Don Bosco 56 esquina Sarmiento"
    )
    assert "direccion_contacto" not in handler.flow_context["datos_reclamo"]
    assert "qa.ubicacion@example.com" in response["message_body"]


def test_complete_legacy_contact_state_recovers_before_image_evidence():
    claim = _complete_claim(direccion="Dirección vigente")
    handler = _handler_for(ReclamoState.ESPERANDO_DATOS_CONTACTO, claim)

    response = handler.handle(
        "",
        {
            "pregunta": "",
            "es_foto": True,
            "es_archivo": True,
            "foto_url": "https://media.example/recovery-evidence.jpg",
            "archivo_id_para_asociar": 473,
        },
    )

    assert handler.flow_context["state"] == ReclamoState.ESPERANDO_CONFIRMACION.name
    assert handler.flow_context["datos_reclamo"]["direccion"] == "Dirección vigente"
    assert handler.flow_context["datos_reclamo"]["foto_url"] == (
        "https://media.example/recovery-evidence.jpg"
    )
    assert handler.flow_context["datos_reclamo"]["archivo_id_para_asociar"] == 473
    assert "Foto agregada" in response["message_body"]


def test_pin_question_during_confirmation_keeps_same_draft_without_ticket():
    claim = _complete_claim()
    expected_claim = deepcopy(claim)
    handler = _handler_for(ReclamoState.ESPERANDO_CONFIRMACION, claim)

    with patch("services.municipio_responder.CrearReclamoActionHandler") as action_handler:
        response = handler.handle(
            "¿Y mi pin para poder preguntar en el futuro?",
            {"pregunta": "¿Y mi pin para poder preguntar en el futuro?"},
        )

    action_handler.assert_not_called()
    assert handler.flow_context["state"] == ReclamoState.ESPERANDO_CONFIRMACION.name
    assert handler.flow_context["datos_reclamo"] == expected_claim
    assert "pin" in response["message_body"].lower()
    assert "todavía no" in response["message_body"].lower()
    assert response["options_list"]


def test_confirmed_audio_claim_hands_callback_request_to_ticket_action():
    handler = _handler_for(
        ReclamoState.ESPERANDO_CONFIRMACION,
        _complete_claim(
            solicita_llamada=True,
            motivo_llamada="Quiere coordinar el reclamo por teléfono.",
        ),
    )

    with patch("services.municipio_responder.CrearReclamoActionHandler") as action_handler:
        action_handler.return_value.execute.return_value = {
            "success": True,
            "message_body": "Reclamo recibido.",
            "message_type": "text",
            "data": {
                "ticket_id": 401,
                "nro_ticket": "M-401",
                "consulta_pin": "167779",
            },
        }
        handler.handle("confirmar", {"pregunta": "confirmar"})

    action_data = action_handler.return_value.execute.call_args.args[0]
    assert action_data["solicita_llamada"] is True
    assert action_data["motivo_llamada"] == "Quiere coordinar el reclamo por teléfono."


def test_ticket_action_persists_callback_as_pending_crm_metadata(client):
    owner = User(
        name="Municipio Callback",
        email="municipio-callback@example.com",
        password_hash="test-hash",
        rol="admin",
        tipo_chat="municipio",
        tenant_slug="municipio-callback",
    )
    db.session.add(owner)
    db.session.flush()
    owner.municipio_id = owner.id
    tenant = TenantProfile(
        slug="municipio-callback",
        nombre="Municipio Callback",
        tipo="municipio",
        municipio_id=owner.id,
        is_active=True,
    )
    db.session.add(tenant)
    db.session.commit()
    context = {
        "channel": "web",
        "user_obj": owner,
        "tenant_profile": tenant,
        "tenant_id": tenant.id,
        "anon_id": "callback-regression-user",
        "municipio_config_actual": {
            "ciudad": "Palmira",
            "base_chat_url": "https://www.chatboc.ar/chat",
        },
        "chat_db_context_data": {CONTEXTO_MUNICIPIO: {}},
    }
    action_data = {
        "categoria": "Luminaria",
        "descripcion": "Poste caído en el barrio.",
        "ubicacion": "Don Bosco 56, Palmira",
        "usuario": "Marcelo",
        "dni": "32877851",
        "email": "marcelo@example.com",
        "telefono": "+5492613168608",
        "solicita_llamada": True,
        "motivo_llamada": "Quiere coordinar el reclamo por teléfono.",
    }

    with (
        patch("services.actions.municipio_actions.validar_email", return_value=True),
        patch("services.actions.municipio_actions.validar_telefono", return_value=True),
        patch("services.actions.municipio_actions.direccion_es_valida", return_value=True),
        patch(
            "services.actions.municipio_actions.formatear_telefono_e164",
            return_value="+5492613168608",
        ),
        patch(
            "services.actions.municipio_actions.parse_direccion",
            return_value={"localidad": "Palmira"},
        ),
        patch(
            "services.actions.municipio_actions.cargar_configuracion_municipio",
            return_value={},
        ),
        patch(
            "services.actions.municipio_actions.formatear_ticket_respuesta",
            return_value=("Reclamo recibido.", []),
        ),
        patch(
            "services.actions.municipio_actions.promo_service.build_ticket_promo_section",
            return_value=None,
        ),
        patch(
            "services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket",
            return_value={"id": 401, "nro_ticket": "401"},
        ) as create_ticket,
    ):
        response = CrearReclamoActionHandler(context).execute(action_data)

    ticket_data = create_ticket.call_args.kwargs["ticket_data"]
    callback = ticket_data["datos_extra"]["callback_request"]
    assert callback == {
        "requested": True,
        "channel": "phone",
        "status": "pending",
        "reason": "Quiere coordinar el reclamo por teléfono.",
    }
    assert response["data"]["callback_request"] == callback
    assert response["contexto_actualizado"]["pending_callback_request"]["ticket_nro"] == "M-401"
