from types import SimpleNamespace
from unittest.mock import patch

from models import ChatSessionContext, MunicipioTicket, db
from routes.whatsapp_webhook import _apply_completed_reclamo_context
from services.actions.municipio_actions import (
    CrearReclamoActionHandler,
    _acquire_municipal_claim_confirmation_lock,
)
from services.constants import CONTEXTO_MUNICIPIO
from services.municipio_responder import ReclamoFlowHandler, ReclamoState


def _claim_data() -> dict:
    return {
        "categoria": "Arbolado",
        "descripcion": "Hay una rama grande caída sobre la vereda.",
        "direccion": "Don Bosco 56, Junín",
        "nombre": "Marcelo Pérez",
        "dni": "32877851",
        "email": "marcelo@example.com",
        "telefono": "+5492613168608",
    }


def _waiting_context(*, confirmation_id: str = "claim-confirmation-001") -> dict:
    return {
        "foto_url": "https://cdn.example.test/stale-photo.jpg",
        CONTEXTO_MUNICIPIO: {
            "estado_conversacion": "EN_FLUJO_RECLAMO",
            "reclamo_flow_v2": {
                "state": ReclamoState.ESPERANDO_CONFIRMACION.name,
                "confirmation_id": confirmation_id,
                "datos_reclamo": _claim_data(),
            },
        },
    }


def test_claim_completion_reloads_expired_session_context_before_cleanup(client):
    stored = ChatSessionContext(
        chat_session_id="wa-claim-integrity-001",
        anon_id="+5492613168608",
        context_data=_waiting_context(),
    )
    db.session.add(stored)
    db.session.commit()

    context = {
        "chat_db_context_data": stored.context_data,
        "foto_url": stored.context_data["foto_url"],
        "es_foto": False,
        "archivo_id_para_asociar": None,
    }
    handler = ReclamoFlowHandler(context, stored)

    def _commit_then_succeed(action_data):
        # This is the exact invalidation boundary from crear_nuevo_ticket:
        # all pre-commit dict references become detached from the ORM
        # attribute.  The stable confirmation id must already be durable so a
        # crash at this boundary can still be replayed safely.
        db.session.commit()
        assert action_data["claim_confirmation_id"] == "claim-confirmation-001"
        db.session.expire(stored, ["context_data"])
        pending = stored.context_data[CONTEXTO_MUNICIPIO]["reclamo_flow_v2"]
        assert pending["confirmation_id"] == "claim-confirmation-001"
        return {
            "success": True,
            "message_body": "Reclamo recibido.",
            "message_type": "text",
            "data": {
                "ticket_id": 407,
                "nro_ticket": "M-401746",
                "consulta_pin": "167779",
                "tracking_url": "https://chatboc.test/tracking/M-401746",
            },
            "contexto_actualizado": {
                "latest_ticket_id": 407,
                "latest_ticket_nro": "M-401746",
                "latest_ticket_pin": "167779",
            },
        }

    with patch("services.municipio_responder.CrearReclamoActionHandler") as action_handler:
        action_handler.return_value.execute.side_effect = _commit_then_succeed
        response = handler.handle_confirmacion("1", {})

    assert response["_reclamo_completion"]["confirmation_id"] == "claim-confirmation-001"
    assert response["contexto_actualizado"]["latest_ticket_id"] == 407
    db.session.commit()
    db.session.expire(stored, ["context_data"])

    persisted = stored.context_data
    municipality = persisted[CONTEXTO_MUNICIPIO]
    assert "reclamo_flow_v2" not in municipality
    assert municipality["estado_conversacion"] == "CONVERSACION_GENERAL_LLM"
    assert municipality["last_created_reclamo"]["ticket_nro"] == "M-401746"
    assert persisted["latest_ticket_id"] == 407
    assert "foto_url" not in persisted
    action_handler.return_value.execute.assert_called_once()


def test_webhook_completion_marker_wins_over_stale_formatter_merge():
    stale = _waiting_context()
    stale["last_options_sent"] = [
        {"texto": "Confirmar", "action_id": "reclamo_confirmar_si"}
    ]
    completion = {
        "confirmation_id": "claim-confirmation-001",
        "ticket_id": 407,
        "ticket_nro": "M-401746",
        "consulta_pin": "167779",
        "tracking_url": "https://chatboc.test/tracking/M-401746",
        "fingerprint": {"descripcion": "rama caida"},
        "contexto_actualizado": {
            "latest_ticket_id": 407,
            "latest_ticket_nro": "M-401746",
            CONTEXTO_MUNICIPIO: {
                "estado_conversacion": "EN_FLUJO_RECLAMO",
                "reclamo_flow_v2": {
                    "state": ReclamoState.ESPERANDO_CONFIRMACION.name,
                },
            },
        },
    }

    merged = _apply_completed_reclamo_context(stale, completion)

    municipality = merged[CONTEXTO_MUNICIPIO]
    assert "reclamo_flow_v2" not in municipality
    assert municipality["estado_conversacion"] == "CONVERSACION_GENERAL_LLM"
    assert municipality["last_created_reclamo"]["confirmation_id"] == "claim-confirmation-001"
    assert municipality["last_created_reclamo"]["fingerprint"] == {
        "descripcion": "rama caida"
    }
    assert merged["latest_ticket_id"] == 407
    assert "foto_url" not in merged


def test_replayed_confirmation_reuses_durable_ticket_without_new_create(client):
    existing = MunicipioTicket(
        pregunta="",
        asunto="Arbolado",
        categoria="Arbolado",
        detalles="Hay una rama grande caída sobre la vereda.",
        direccion="Don Bosco 56, Junín",
        municipio_id=123,
        anon_id="+5492613168608",
        nro_ticket="401746",
        consulta_pin="167779",
        nombre_vecino="Marcelo Pérez",
        telefono_vecino="+5492613168608",
        email_vecino="marcelo@example.com",
        dni_vecino="32877851",
        datos_extra={
            "whatsapp_claim_confirmation_id": "claim-confirmation-001",
        },
    )
    db.session.add(existing)
    db.session.commit()

    owner = SimpleNamespace(
        id=123,
        municipio_id=123,
        link_web=None,
        telefono=None,
        horario=None,
    )
    context = {
        "user_obj": owner,
        "viewer_user_obj": None,
        "anon_id": "+5492613168608",
        "channel": "whatsapp",
        "municipio_config_actual": {
            "base_chat_url": "https://www.chatboc.ar/chat",
        },
        "chat_db_context_data": {CONTEXTO_MUNICIPIO: {}},
    }
    action_data = {
        "categoria": "Arbolado",
        "descripcion": "Hay una rama grande caída sobre la vereda.",
        "ubicacion": "Don Bosco 56, Junín",
        "usuario": "Marcelo Pérez",
        "dni": "32877851",
        "email": "marcelo@example.com",
        "telefono": "+5492613168608",
        "claim_confirmation_id": "claim-confirmation-001",
    }

    with (
        patch("services.actions.municipio_actions.validar_email", return_value=True),
        patch("services.actions.municipio_actions.validar_telefono", return_value=True),
        patch(
            "services.actions.municipio_actions.formatear_telefono_e164",
            return_value="+5492613168608",
        ),
        patch(
            "services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket"
        ) as create_ticket,
    ):
        responses = [
            CrearReclamoActionHandler(context).execute(action_data)
            for _ in range(3)
        ]

    assert MunicipioTicket.query.count() == 1
    for response in responses:
        assert response["success"] is True
        assert response["data"]["deduplicated"] is True
        assert response["data"]["ticket_id"] == existing.id
        assert response["data"]["nro_ticket"] == "M-401746"
        assert response["data"]["consulta_pin"] == "167779"
    create_ticket.assert_not_called()


def test_postgres_confirmation_lock_is_stable_and_transaction_scoped():
    class FakeSession:
        def __init__(self):
            self.calls = []

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def execute(self, statement, parameters):
            self.calls.append((str(statement), parameters))

        def rollback(self):
            raise AssertionError("rollback should not be called")

    first = FakeSession()
    second = FakeSession()
    kwargs = {
        "confirmation_id": "claim-confirmation-001",
        "tenant_id": 22,
    }

    assert _acquire_municipal_claim_confirmation_lock(first, **kwargs) is True
    assert _acquire_municipal_claim_confirmation_lock(second, **kwargs) is True
    assert first.calls[0][0] == "SELECT pg_advisory_xact_lock(:lock_id)"
    assert first.calls[0][1] == second.calls[0][1]
    assert isinstance(first.calls[0][1]["lock_id"], int)


def test_confirmation_lock_refuses_unscoped_replay():
    session = SimpleNamespace()

    assert (
        _acquire_municipal_claim_confirmation_lock(
            session,
            confirmation_id="claim-confirmation-001",
        )
        is False
    )


def test_legacy_confirmation_without_id_uses_same_lock_identity_for_same_snapshot():
    captured_ids = []

    def _fail_after_capture(action_data):
        captured_ids.append(action_data["claim_confirmation_id"])
        return {
            "success": False,
            "message_to_user": "No se creó ningún ticket.",
        }

    with patch("services.municipio_responder.CrearReclamoActionHandler") as action_handler:
        action_handler.return_value.execute.side_effect = _fail_after_capture
        for _ in range(2):
            legacy_context = _waiting_context(confirmation_id="")
            legacy_context[CONTEXTO_MUNICIPIO]["reclamo_flow_v2"].pop(
                "confirmation_id",
                None,
            )
            handler = ReclamoFlowHandler(
                {
                    "chat_db_context_data": legacy_context,
                    "chat_session_uuid": "wa-legacy-session-001",
                    "anon_id": "+5492613168608",
                },
                chat_db_context=None,
            )
            handler.handle_confirmacion("1", {})

    assert len(captured_ids) == 2
    assert captured_ids[0] == captured_ids[1]
    assert captured_ids[0].startswith("legacy-")


def test_pin_question_at_confirmation_does_not_create_ticket():
    context_data = _waiting_context()
    context = {
        "chat_db_context_data": context_data,
        "foto_url": None,
        "es_foto": False,
    }
    handler = ReclamoFlowHandler(context, chat_db_context=None)

    with patch("services.municipio_responder.CrearReclamoActionHandler") as action_handler:
        response = handler.handle_confirmacion(
            "¿Y mi PIN para poder consultar el reclamo en el futuro?",
            {},
        )

    assert "Todavía no creé ningún ticket" in response["message_body"]
    assert handler.flow_context["state"] == ReclamoState.ESPERANDO_CONFIRMACION.name
    action_handler.assert_not_called()
