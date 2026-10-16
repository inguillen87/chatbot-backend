from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import jwt
import pytest
from twilio.request_validator import RequestValidator


os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("SKIP_INIT_TENANTS", "1")

from app import create_app
from config import Config
from extensions import db
from models import (
    ArchivoAdjunto,
    AuditEvent,
    ChatSessionContext,
    DomainEffectOutbox,
    MessagingEventLedger,
    MunicipioTicket,
    MunicipioTicketReplyEvent,
    ProviderConnection,
    ProviderSender,
    TenantProfile,
    TicketComentario,
    User,
    WhatsappNumero,
    WhatsAppContactState,
    WhatsAppInboundTurn,
)
from routes import whatsapp_webhook as webhook_module
from services import whatsapp_inbound_worker as worker_module
from services.constants import CONTEXTO_MUNICIPIO
from services.municipio_responder import ReclamoState
from tests.junin_product_flow_support import JUNIN_QA_LAT, JUNIN_QA_LNG


ACCOUNT_SID = "ACjunin_golden_path"
AUTH_TOKEN = "junin-golden-path-auth-token"
TOKEN_REF = "TWILIO_SUBACCOUNT_AUTH_TOKEN_JUNIN_GOLDEN_PATH"
MESSAGING_SERVICE_SID = "MG_junin_golden_path"
SENDER_NUMBER = "+15550106060"
CITIZEN_NUMBER = "+5492615550188"
CITIZEN_NAME = "Vecina Golden Path"
CITIZEN_DNI = "32877851"
CITIZEN_EMAIL = "vecina.golden.path@example.test"
CLAIM_ADDRESS = "Rivadavia 100, Ciudad de Junín, Mendoza"
PRIVATE_MEDIA_URL = "https://api.twilio.test/private/luminaria.jpg?token=private-media-token"
OPERATOR_REPLY = "La cuadrilla de Luminarias tomó el reclamo y revisará el poste hoy."


class JuninGoldenPathConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {
        "connect_args": {"check_same_thread": False, "timeout": 5}
    }
    WTF_CSRF_ENABLED = False
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False
    SKIP_INIT_TENANTS = True
    TWILIO_ACCOUNT_SID = "ACunused_parent"
    TWILIO_AUTH_TOKEN = "unused-parent-token"
    TWILIO_ALLOW_NETWORK_IN_TESTS = True
    PUBLIC_API_BASE_URL = "http://localhost"
    BACKEND_URL = "http://localhost"
    WHATSAPP_AUDIO_ENABLED = False
    WHATSAPP_INBOUND_DURABILITY_MODE = "queue"
    WHATSAPP_INBOUND_HASH_SECRET = "junin-golden-path-stream-secret-32-bytes"
    CHANNEL_SESSION_IDENTITY_MODE = "enforce"
    CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1 = (
        "junin-golden-path-session-secret-32-bytes"
    )
    CHANNEL_SESSION_IDENTITY_VERSION_V1 = "v1"
    WHATSAPP_INBOUND_LEASE_SECONDS = 180
    WHATSAPP_INBOUND_MAX_ATTEMPTS = 4
    WHATSAPP_INBOUND_WORKER_BATCH_SIZE = 4
    DOMAIN_EFFECT_OUTBOX_MODE = "queue"
    DOMAIN_EFFECT_OUTBOX_SECRET = "junin-golden-path-outbox-secret-32-bytes"
    DOMAIN_EFFECT_OUTBOX_MAX_PAYLOAD_BYTES = 4096
    DOMAIN_EFFECT_OUTBOX_MAX_ATTEMPTS = 8
    EMAIL_NOTIFICATIONS_ENABLED = False
    ENABLE_EMAIL_NOTIFICATIONS = False


@pytest.fixture()
def junin_journey():
    app = create_app(JuninGoldenPathConfig)
    app.config[TOKEN_REF] = AUTH_TOKEN
    ctx = app.app_context()
    ctx.push()
    db.create_all()
    client = app.test_client()

    owner = User(
        name="Municipalidad de Junín QA",
        email="owner-junin-golden@example.test",
        rol="admin",
        tipo_chat="municipio",
        tenant_slug="junin",
    )
    owner.set_password("secret123")
    db.session.add(owner)
    db.session.flush()

    tenant = TenantProfile(
        slug="junin",
        nombre="Municipalidad de Junín QA",
        tipo="municipio",
        vertical="gobierno",
        plan="full",
        municipio_id=owner.id,
        is_active=True,
        configuracion={
            "twilio_tech_provider": {
                "twilio_account_sid": ACCOUNT_SID,
                "twilio_subaccount_token_ref": TOKEN_REF,
                "messaging_service_sid": MESSAGING_SERVICE_SID,
            }
        },
    )
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id = tenant.id

    citizen = User(
        name=CITIZEN_NAME,
        email=CITIZEN_EMAIL,
        telefono=CITIZEN_NUMBER,
        rol="usuario",
        tipo_chat="municipio",
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
    )
    citizen.set_password("secret123")
    luminarias = User(
        name="Operador Luminarias",
        email="operador-luminarias@example.test",
        rol="empleado",
        tipo_chat="municipio",
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        es_empleado=True,
        accesibilidad={
            "employee_scope": {
                "categorias": ["Luminarias"],
                "channels": ["whatsapp"],
                "permisos": [],
            }
        },
    )
    luminarias.set_password("secret123")
    bacheo = User(
        name="Operador Bacheo",
        email="operador-bacheo@example.test",
        rol="empleado",
        tipo_chat="municipio",
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        es_empleado=True,
        accesibilidad={
            "employee_scope": {
                "categorias": ["Bacheo"],
                "channels": ["whatsapp"],
                "permisos": [],
            }
        },
    )
    bacheo.set_password("secret123")
    db.session.add_all([citizen, luminarias, bacheo])
    db.session.flush()

    connection = ProviderConnection(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        environment="production",
        status="online",
        external_account_id=ACCOUNT_SID,
        credentials_ref=f"env:{TOKEN_REF}",
    )
    db.session.add(connection)
    db.session.flush()
    sender = ProviderSender(
        tenant_id=tenant.id,
        provider_connection_id=connection.id,
        channel="whatsapp",
        sender_type="whatsapp_business",
        phone_number=SENDER_NUMBER,
        sender_id=f"whatsapp:{SENDER_NUMBER}",
        messaging_service_sid=MESSAGING_SERVICE_SID,
        status="online",
        status_callback_url="http://localhost/twilio/whatsapp/status",
    )
    db.session.add(sender)
    db.session.flush()
    db.session.add_all(
        [
            WhatsappNumero(
                numero_whatsapp=SENDER_NUMBER,
                user_id=owner.id,
                is_active=True,
            ),
            WhatsAppContactState(
                tenant_id=tenant.id,
                provider_sender_id=sender.id,
                recipient=f"whatsapp:{CITIZEN_NUMBER}",
                last_inbound_at=datetime.now(timezone.utc),
            ),
        ]
    )
    db.session.commit()

    app.config["WHATSAPP_INBOUND_QUEUE_TENANT_IDS"] = str(tenant.id)
    app.config["DOMAIN_EFFECT_OUTBOX_TENANT_IDS"] = str(tenant.id)

    def auth(user: User) -> dict[str, str]:
        token = jwt.encode(
            {
                "user_id": user.id,
                "rol": user.rol,
                "tenant_slug": tenant.slug,
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        return {
            "Authorization": f"Bearer {token}",
            "X-Tenant-Slug": tenant.slug,
        }

    try:
        yield SimpleNamespace(
            app=app,
            client=client,
            owner=owner,
            tenant=tenant,
            citizen=citizen,
            luminarias=luminarias,
            bacheo=bacheo,
            sender=sender,
            auth=auth,
        )
    finally:
        db.session.remove()
        db.drop_all()
        ctx.pop()


def _payload(sid: str, **overrides: str) -> dict[str, str]:
    payload = {
        "AccountSid": ACCOUNT_SID,
        "MessagingServiceSid": MESSAGING_SERVICE_SID,
        "To": f"whatsapp:{SENDER_NUMBER}",
        "From": f"whatsapp:{CITIZEN_NUMBER}",
        "WaId": CITIZEN_NUMBER.removeprefix("+"),
        "ProfileName": CITIZEN_NAME,
        "Body": "",
        "MessageSid": sid,
        "SmsMessageSid": sid,
        "NumMedia": "0",
    }
    payload.update(overrides)
    return payload


def _post_signed_retry(client, payload: dict[str, str]):
    signature = RequestValidator(AUTH_TOKEN).compute_signature(
        "http://localhost/webhook/whatsapp",
        payload,
    )
    first = client.post(
        "/webhook/whatsapp",
        data=payload,
        headers={
            "X-Twilio-Signature": signature,
            "I-Twilio-Idempotency-Token": "first-delivery",
        },
    )
    retry = client.post(
        "/webhook/whatsapp",
        data=payload,
        headers={
            "X-Twilio-Signature": signature,
            "I-Twilio-Idempotency-Token": "provider-retry",
        },
    )
    assert first.status_code == retry.status_code == 200
    assert first.get_data(as_text=True) == retry.get_data(as_text=True) == "OK"
    return WhatsAppInboundTurn.query.filter_by(
        provider_message_sid=payload["MessageSid"]
    ).one()


def _process_one_inbound_turn(app, *, tenant_id: int, stream_key: str):
    # The durable worker owns its application context in production. Keeping
    # that boundary here prevents its internal replay marker on ``flask.g``
    # from leaking into the next independent test-client webhook request.
    with app.app_context():
        return worker_module.process_whatsapp_inbound_stream(
            tenant_id=tenant_id,
            stream_key=stream_key,
            limit=1,
        )


def test_junin_whatsapp_claim_assignment_and_reply_golden_path(
    junin_journey,
    caplog,
):
    env = junin_journey
    caplog.set_level(logging.DEBUG)
    caplog.clear()
    provider_client = MagicMock()

    stored_attachments: list[int] = []

    def store_photo(file_storage, user_id=None, session_id=None):
        content = file_storage.stream.getvalue()
        attachment = ArchivoAdjunto(
            user_id=user_id,
            session_id=session_id,
            filename=file_storage.filename,
            nombre_original="luminaria-caida.jpg",
            mime=file_storage.content_type,
            tamano=len(content),
            tipo="chat_adjunto",
            url="https://cdn.example.test/junin/luminaria-caida.jpg",
        )
        db.session.add(attachment)
        db.session.flush()
        stored_attachments.append(attachment.id)
        return attachment

    reverse_geocoded = {
        "formatted_address": CLAIM_ADDRESS,
        "calle": "Rivadavia",
        "numero": "100",
        "localidad": "Junín",
        "provincia": "Mendoza",
    }

    with (
        patch.object(webhook_module, "Client", return_value=provider_client),
        patch.object(
            webhook_module,
            "_download_twilio_media",
            return_value=b"\xff\xd8\xff controlled-junin-photo",
        ) as download_media,
        patch.object(
            webhook_module,
            "create_attachment_with_thumbnail",
            side_effect=store_photo,
        ) as persist_media,
        patch.object(
            webhook_module,
            "clasificar_adjunto_whatsapp",
            return_value={"error": "controlled_vision_boundary"},
        ),
        patch.object(worker_module, "enqueue_whatsapp_inbound_stream") as enqueue_turn,
        patch(
            "services.domain_effect_worker.enqueue_domain_effect_dispatch",
            return_value=True,
        ),
        patch(
            "services.herramientas_municipio.obtener_direccion_de_coordenadas",
            return_value=reverse_geocoded,
        ),
        patch(
            "services.actions.municipio_actions.direccion_es_valida",
            return_value=True,
        ),
        patch(
            "services.actions.municipio_actions.parse_direccion",
            return_value={"localidad": "Junín", "provincia": "Mendoza"},
        ),
        patch(
            "services.actions.municipio_actions.servicio_tickets.auto_assign_enabled",
            False,
        ),
    ):
        location_payload = _payload(
            "SM" + ("1" * 32),
            Latitude=str(JUNIN_QA_LAT),
            Longitude=str(JUNIN_QA_LNG),
            Address=CLAIM_ADDRESS,
        )
        unsigned = env.client.post("/webhook/whatsapp", data=location_payload)
        assert unsigned.status_code == 403
        assert WhatsAppInboundTurn.query.count() == 0

        location_turn = _post_signed_retry(env.client, location_payload)
        assert WhatsAppInboundTurn.query.filter_by(
            provider_message_sid=location_payload["MessageSid"]
        ).count() == 1
        enqueue_turn.assert_called_once_with(
            tenant_id=env.tenant.id,
            stream_key=location_turn.stream_key,
        )

        session = db.session.get(ChatSessionContext, location_turn.chat_session_id)
        assert session is not None
        session.user_id = env.citizen.id
        session.anon_id = CITIZEN_NUMBER
        session.context_data = {
            "perfil_confirmado": True,
            CONTEXTO_MUNICIPIO: {
                "estado_conversacion": "EN_FLUJO_RECLAMO",
                "reclamo_flow_v2": {
                    "state": ReclamoState.ESPERANDO_DIRECCION.name,
                    "confirmation_id": "junin-golden-claim-0001",
                    "datos_reclamo": {
                        "categoria": "Luminarias",
                        "descripcion": "Una luminaria cayó y dejó el poste inclinado sobre la vereda.",
                        "nombre": CITIZEN_NAME,
                        "dni": CITIZEN_DNI,
                        "email": CITIZEN_EMAIL,
                        "telefono": CITIZEN_NUMBER,
                    },
                },
            },
        }
        db.session.add(session)
        db.session.commit()

        location_result = _process_one_inbound_turn(
            env.app,
            tenant_id=env.tenant.id,
            stream_key=location_turn.stream_key,
        )
        assert location_result["completed"] == 1, location_result
        db.session.expire_all()
        session = db.session.get(ChatSessionContext, location_turn.chat_session_id)
        location_draft = session.context_data[CONTEXTO_MUNICIPIO][
            "reclamo_flow_v2"
        ]["datos_reclamo"]
        assert location_draft["direccion"] == CLAIM_ADDRESS
        assert location_draft["coordenadas"] == {
            "lat": JUNIN_QA_LAT,
            "lng": JUNIN_QA_LNG,
        }
        assert session.context_data[CONTEXTO_MUNICIPIO]["reclamo_flow_v2"][
            "state"
        ] == ReclamoState.ESPERANDO_FOTO.name

        photo_payload = _payload(
            "SM" + ("2" * 32),
            NumMedia="1",
            MediaMessageSid="MM" + ("2" * 32),
            MediaUrl0=PRIVATE_MEDIA_URL,
            MediaContentType0="image/jpeg",
        )
        photo_turn = _post_signed_retry(env.client, photo_payload)
        assert photo_turn.stream_key == location_turn.stream_key
        photo_result = _process_one_inbound_turn(
            env.app,
            tenant_id=env.tenant.id,
            stream_key=photo_turn.stream_key,
        )
        assert photo_result["completed"] == 1, photo_result
        download_media.assert_called_once()
        persist_media.assert_called_once()
        assert len(stored_attachments) == 1
        db.session.expire_all()
        session = db.session.get(ChatSessionContext, photo_turn.chat_session_id)
        photo_flow = session.context_data[CONTEXTO_MUNICIPIO]["reclamo_flow_v2"]
        assert photo_flow["state"] == ReclamoState.ESPERANDO_CONFIRMACION.name
        assert photo_flow["datos_reclamo"]["archivo_id_para_asociar"] == stored_attachments[0]
        assert photo_flow["datos_reclamo"]["foto_url"].endswith(
            "/luminaria-caida.jpg"
        )

        confirmation_payload = _payload(
            "SM" + ("3" * 32),
            Body="Confirmar",
        )
        confirmation_turn = _post_signed_retry(env.client, confirmation_payload)
        assert confirmation_turn.stream_key == location_turn.stream_key
        confirmation_result = _process_one_inbound_turn(
            env.app,
            tenant_id=env.tenant.id,
            stream_key=confirmation_turn.stream_key,
        )
        replay_worker_result = _process_one_inbound_turn(
            env.app,
            tenant_id=env.tenant.id,
            stream_key=confirmation_turn.stream_key,
        )

    assert confirmation_result["completed"] == 1, confirmation_result
    assert replay_worker_result["processed"] == 0, replay_worker_result
    assert provider_client.messages.create.call_count == 0
    db.session.expire_all()
    assert WhatsAppInboundTurn.query.count() == 3
    assert all(
        turn.status == WhatsAppInboundTurn.STATUS_COMPLETED
        for turn in WhatsAppInboundTurn.query.all()
    )

    tickets = MunicipioTicket.query.filter_by(tenant_id=env.tenant.id).all()
    assert len(tickets) == 1
    ticket = tickets[0]
    assert ticket.categoria == "Luminarias"
    assert ticket.direccion == CLAIM_ADDRESS
    assert ticket.latitud == pytest.approx(JUNIN_QA_LAT)
    assert ticket.longitud == pytest.approx(JUNIN_QA_LNG)
    assert ticket.canal_ingreso == "whatsapp"
    assert ticket.telefono_vecino == CITIZEN_NUMBER
    assert ticket.foto_url_directa == "https://cdn.example.test/junin/luminaria-caida.jpg"
    assert ticket.archivos.count() == 1
    assert ticket.archivos.one().id == stored_attachments[0]
    assert ticket.asignado_a_id is None

    denied_claim = env.client.post(
        f"/api/v2/inbox/omnichannel/{ticket.id}/actions",
        json={"action": "claim", "source_model": "MunicipioTicket"},
        headers=env.auth(env.bacheo),
    )
    assert denied_claim.status_code == 404, denied_claim.get_json()
    db.session.refresh(ticket)
    assert ticket.asignado_a_id is None

    accepted_claim = env.client.post(
        f"/api/v2/inbox/omnichannel/{ticket.id}/actions",
        json={"action": "claim", "source_model": "MunicipioTicket"},
        headers=env.auth(env.luminarias),
    )
    assert accepted_claim.status_code == 200, accepted_claim.get_json()
    assert accepted_claim.get_json()["delivery"]["status"] == "claimed"
    db.session.expire_all()
    ticket = db.session.get(MunicipioTicket, ticket.id)
    assert ticket.asignado_a_id == env.luminarias.id
    assert TicketComentario.query.filter_by(
        municipio_ticket_id=ticket.id,
        user_id=env.luminarias.id,
        origen="admin_panel",
    ).count() == 1

    reply_key = "junin-golden-reply-0001"
    reply_payload = {
        "action": "reply",
        "source_model": "MunicipioTicket",
        "body": OPERATOR_REPLY,
        "visibility": "public",
        "delivery_channels": ["whatsapp"],
        "client_message_id": reply_key,
    }
    reply_headers = {
        **env.auth(env.luminarias),
        "Idempotency-Key": reply_key,
    }
    with patch(
        "services.domain_effect_worker.enqueue_domain_effect_dispatch",
        return_value=True,
    ) as enqueue_reply_effect:
        first_reply = env.client.post(
            f"/api/v2/inbox/omnichannel/{ticket.id}/actions",
            json=reply_payload,
            headers=reply_headers,
        )
        replayed_reply = env.client.post(
            f"/api/v2/inbox/omnichannel/{ticket.id}/actions",
            json=reply_payload,
            headers=reply_headers,
        )
    assert first_reply.status_code == replayed_reply.status_code == 200
    assert enqueue_reply_effect.called
    assert first_reply.get_json()["delivery"]["mode"] == "durable_queue"
    assert replayed_reply.get_json()["delivery"]["idempotency"]["replayed"] is True
    assert TicketComentario.query.filter_by(
        municipio_ticket_id=ticket.id,
        comentario=OPERATOR_REPLY,
    ).count() == 1
    reply_record = MunicipioTicketReplyEvent.query.filter_by(
        tenant_id=env.tenant.id,
        ticket_id=ticket.id,
    ).one()
    external_reply_outbox = DomainEffectOutbox.query.filter_by(
        tenant_id=env.tenant.id,
        aggregate_type="municipio_ticket_reply",
        aggregate_ref=f"{ticket.id}:{reply_record.event_id}",
        channel="whatsapp",
    ).all()
    assert len(external_reply_outbox) == 1
    assert external_reply_outbox[0].status == DomainEffectOutbox.STATUS_PENDING
    assert CITIZEN_NUMBER not in json.dumps(
        external_reply_outbox[0].payload_json,
        ensure_ascii=False,
        sort_keys=True,
    )

    callback_path = (
        "/twilio/whatsapp/status?municipio_ticket_reply_event_id="
        f"{reply_record.id}"
    )
    provider_message_sid = "SM" + ("d" * 32)
    callback_payload = {
        "AccountSid": ACCOUNT_SID,
        "MessagingServiceSid": MESSAGING_SERVICE_SID,
        "MessageSid": provider_message_sid,
        "MessageStatus": "delivered",
        "From": f"whatsapp:{SENDER_NUMBER}",
        "To": f"whatsapp:{CITIZEN_NUMBER}",
    }
    callback_signature = RequestValidator(AUTH_TOKEN).compute_signature(
        f"http://localhost{callback_path}",
        callback_payload,
    )
    callback_headers = {"X-Twilio-Signature": callback_signature}
    delivered = env.client.post(
        callback_path,
        data=callback_payload,
        headers=callback_headers,
    )
    callback_replay = env.client.post(
        callback_path,
        data=callback_payload,
        headers=callback_headers,
    )
    assert delivered.status_code == callback_replay.status_code == 200

    db.session.expire_all()
    reply_record = db.session.get(MunicipioTicketReplyEvent, reply_record.id)
    assert reply_record.whatsapp_delivery_status == "delivered"
    assert reply_record.whatsapp_provider_status == "delivered"
    assert reply_record.whatsapp_provider_message_id == provider_message_sid
    assert reply_record.whatsapp_delivered_at is not None
    assert MessagingEventLedger.query.filter_by(
        tenant_id=env.tenant.id,
        provider_event_id=f"{provider_message_sid}:delivered",
    ).count() == 1
    callback_audits = AuditEvent.query.filter_by(
        tenant_id=env.tenant.id,
        event_type="municipio_ticket.reply.whatsapp.delivery_callback",
    ).all()
    assert len(callback_audits) == 1

    audit_json = json.dumps(
        [event.details for event in callback_audits],
        ensure_ascii=False,
        sort_keys=True,
    )
    rendered_logs = caplog.text
    for private_value in (
        CITIZEN_NAME,
        CITIZEN_DNI,
        CITIZEN_EMAIL,
        CITIZEN_NUMBER,
        CLAIM_ADDRESS,
        PRIVATE_MEDIA_URL,
        "private-media-token",
        str(JUNIN_QA_LAT),
        str(JUNIN_QA_LNG),
    ):
        assert private_value not in audit_json
        assert private_value not in rendered_logs
