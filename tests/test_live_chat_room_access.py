import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import jwt

from app import create_app, db
from config import TestConfig
from models import ChatSessionContext, DomainEffectOutbox, EncEncuesta, EncLink, MunicipioTicket, PymeTicket, Rubro, TenantProfile, TenantTicket, TicketComentario, User
from services.live_chat_access import (
    LIVE_CHAT_TOKEN_AUDIENCE,
    LIVE_CHAT_TOKEN_ISSUER,
    LIVE_CHAT_TOKEN_SCOPE,
    build_ticket_room,
    issue_ticket_room_token,
    verify_ticket_room_token,
)
from services.omnichannel_message_policy import OMNICHANNEL_REPLY_MAX_BODY_BYTES
from routes.whatsapp_webhook import _find_live_chat_ticket
from services.pymes import (
    CONTEXTO_PYME,
    HumanHandler as LegacyPymeHumanHandler,
    UnclearHandler as LegacyPymeUnclearHandler,
    _resolve_pyme_chat_persistence_ticket_id,
)
from services.ticket_service import servicio_tickets
from services.ticket_domain_effects import (
    COMMENT_REQUESTER_WHATSAPP_HANDLER,
    PYME_COMMENT_AGGREGATE,
    emit_tenant_ticket_reply_realtime,
)
from socket_service import (
    _get_rooms_for_user,
    disconnect_clerk_session_sockets,
    emit_ticket_assignment_changed,
    emit_ticket_status_changed,
    handle_send_chat_message,
    on_connect,
    on_join,
    on_location,
    on_new_chat,
    on_subscribe_ticket_updates,
    socketio,
)
from utils.auth_helpers import bump_auth_session_version


class LiveChatRoomAccessTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        self.admin = User(
            name="Admin",
            email="socket-room-admin@example.com",
            rol="admin",
            tipo_chat="municipio",
            municipio_id=910,
        )
        self.admin.set_password("pass")
        db.session.add(self.admin)
        db.session.flush()

        self.ticket = MunicipioTicket(
            municipio_id=910,
            pregunta="Necesito asistencia",
            estado="esperando_agente_en_vivo",
            nro_ticket="LIVE-910",
            anon_id="visitor-910",
        )
        db.session.add(self.ticket)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def _revoked_clerk_token(self):
        self.admin.accesibilidad = {
            "auth": {
                "provider": "clerk",
                "session_version": 1,
                "clerk": {"user_id": "user_socket_admin"},
            }
        }
        db.session.commit()
        now = datetime.now(timezone.utc)
        token = jwt.encode(
            {
                "user_id": self.admin.id,
                "rol": self.admin.rol,
                "auth_provider": "clerk",
                "session_kind": "clerk",
                "sid": "sess_socket_revoked",
                "clerk_sid": "sess_socket_revoked",
                "jti": "jti_socket_revoked",
                "sv": 1,
                "iat": now,
                "exp": now + timedelta(hours=1),
            },
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        bump_auth_session_version(self.admin)
        db.session.commit()
        return token

    def test_revoked_clerk_session_cannot_connect_socket(self):
        token = self._revoked_clerk_token()

        with patch(
            "socket_service.request", SimpleNamespace(sid="revoked-connect")
        ), patch("socket_service.join_room") as join_room:
            result = on_connect({"token": token})

        self.assertFalse(result)
        join_room.assert_not_called()

    def test_revoked_clerk_session_cannot_subscribe_socket(self):
        token = self._revoked_clerk_token()

        with patch(
            "socket_service.request", SimpleNamespace(sid="revoked-subscribe")
        ), patch("socket_service.join_room") as join_room, patch(
            "socket_service.emit"
        ) as emit:
            on_subscribe_ticket_updates({"token": token})

        join_room.assert_not_called()
        emit.assert_called_once_with("subscription_error", {"error": "invalid_token"})

    def test_revoked_clerk_session_cannot_send_socket_message(self):
        token = self._revoked_clerk_token()

        with patch(
            "socket_service.servicio_tickets.crear_comentario"
        ) as create_comment, patch("socket_service.emit") as emit:
            handle_send_chat_message(
                {
                    "token": token,
                    "room": build_ticket_room("municipio", self.ticket.id),
                    "ticket_id": self.ticket.id,
                    "ticket_type": "municipio",
                    "message": "No debe persistirse",
                }
            )

        create_comment.assert_not_called()
        emit.assert_called_once_with("chat_error", {"error": "invalid_token"})

    def test_authenticated_clerk_socket_joins_session_and_user_rooms(self):
        self.admin.accesibilidad = {
            "auth": {
                "provider": "clerk",
                "session_version": 1,
                "clerk": {"user_id": "user_socket_identity"},
            }
        }
        db.session.commit()
        now = datetime.now(timezone.utc)
        token = jwt.encode(
            {
                "user_id": self.admin.id,
                "rol": self.admin.rol,
                "auth_provider": "clerk",
                "session_kind": "clerk",
                "sid": "sess_socket_identity",
                "clerk_sid": "sess_socket_identity",
                "clerk_user_id": "user_socket_identity",
                "jti": "jti_socket_identity",
                "sv": 1,
                "iat": now,
                "exp": now + timedelta(hours=1),
            },
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )

        with patch(
            "socket_service.request", SimpleNamespace(sid="active-clerk-connect")
        ), patch("socket_service.join_room") as join_room:
            result = on_connect({"token": token})

        self.assertIsNone(result)
        joined_rooms = {item.args[0] for item in join_room.call_args_list}
        self.assertIn("clerk_session:sess_socket_identity", joined_rooms)
        self.assertIn("clerk_user:user_socket_identity", joined_rooms)

    def test_cross_category_employee_receives_only_opaque_tenant_invalidation(self):
        tenant = TenantProfile(
            slug="category-socket-scope",
            nombre="Category socket scope",
            tipo="municipio",
            municipio_id=self.admin.id,
        )
        db.session.add(tenant)
        db.session.flush()
        self.admin.tenant_id = tenant.id
        self.admin.tenant_slug = tenant.slug
        employee = User(
            name="Alumbrado operator",
            email="alumbrado-socket@example.com",
            rol="empleado",
            tipo_chat="municipio",
            municipio_id=self.admin.id,
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
            accesibilidad={"employee_scope": {"categorias": ["alumbrado"]}},
        )
        employee.set_password("pass")
        db.session.add(employee)
        db.session.flush()
        restricted_event = {
            "id": "health-reply-event",
            "origin": "admin_panel",
            "action": "reply",
            "body": "health-reply-body-marker",
            "visibility": "public",
            "created_at": "2026-08-20T12:00:00+00:00",
            "actor": {
                "id": self.admin.id,
                "name": "health-reply-actor-marker",
                "role": "admin",
            },
        }
        restricted_ticket = TenantTicket(
            tenant_id=tenant.id,
            user_id=self.admin.id,
            categoria="salud",
            descripcion="Restricted health ticket",
            estado="en_proceso",
            origen="whatsapp",
            datos_extra={"title": "Restricted health ticket", "comments": [restricted_event]},
        )
        allowed_ticket = TenantTicket(
            tenant_id=tenant.id,
            user_id=self.admin.id,
            categoria="alumbrado",
            descripcion="Allowed lighting ticket",
            estado="nuevo",
            origen="web",
            datos_extra={"title": "Allowed lighting ticket"},
        )
        db.session.add_all([restricted_ticket, allowed_ticket])
        db.session.commit()

        employee_token = jwt.encode(
            {"user_id": employee.id, "tenant_id": tenant.id, "tenant_slug": tenant.slug},
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        employee_headers = {
            "Authorization": f"Bearer {employee_token}",
            "X-Tenant-Slug": tenant.slug,
        }
        admin_token = jwt.encode(
            {"user_id": self.admin.id, "tenant_id": tenant.id, "tenant_slug": tenant.slug},
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        admin_headers = {
            "Authorization": f"Bearer {admin_token}",
            "X-Tenant-Slug": tenant.slug,
        }

        self.assertIn(f"tenant_{tenant.id}", _get_rooms_for_user(employee))
        denied = self.client.get(
            f"/api/v2/tickets/{restricted_ticket.id}",
            headers=employee_headers,
        )
        allowed = self.client.get(
            f"/api/v2/tickets/{allowed_ticket.id}",
            headers=employee_headers,
        )
        admin_refetch = self.client.get(
            f"/api/v2/tickets/{restricted_ticket.id}",
            headers=admin_headers,
        )
        self.assertEqual(denied.status_code, 404, denied.get_json())
        self.assertEqual(allowed.status_code, 200, allowed.get_json())
        self.assertEqual(admin_refetch.status_code, 200, admin_refetch.get_json())
        self.assertIn("health-reply-body-marker", str(admin_refetch.get_json()))

        employee_socket = socketio.test_client(
            self.app,
            auth={
                "token": employee_token,
                "tenant_slug": tenant.slug,
            },
        )
        self.assertTrue(employee_socket.is_connected())
        employee_socket.get_received()
        emit_tenant_ticket_reply_realtime(restricted_ticket, restricted_event)
        received = employee_socket.get_received()
        employee_socket.disconnect()

        self.assertEqual([item["name"] for item in received], ["ticket_update"])
        tenant_event = received[0]["args"][0]
        self.assertEqual(
            tenant_event,
            {
                "contract_version": "tickets.collection.invalidated.v1",
                "resource": "tickets",
                "reason": "collection_changed",
                "refetch": True,
            },
        )
        serialized = str(tenant_event)
        self.assertNotIn("health-reply-body-marker", serialized)
        self.assertNotIn("health-reply-actor-marker", serialized)
        self.assertNotIn("ticket_id", tenant_event)
        self.assertNotIn("ticketId", tenant_event)

    def test_http_only_cookie_authenticates_socket_connect_and_subscription(self):
        token = jwt.encode(
            {"user_id": self.admin.id},
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        own_tenant = TenantProfile(
            slug="cookie-municipality",
            nombre="Cookie municipality",
            tipo="municipio",
            municipio_id=910,
        )
        db.session.add(own_tenant)
        db.session.commit()
        cookie_request = SimpleNamespace(
            sid="cookie-authenticated-socket",
            cookies={self.app.config["AUTH_TOKEN_COOKIE_NAME"]: token},
        )

        with patch("socket_service.request", cookie_request), patch(
            "socket_service.join_room"
        ) as join_room, patch("socket_service.emit") as emit:
            connect_result = on_connect({"tenant_slug": own_tenant.slug})
            on_subscribe_ticket_updates({"tenant_slug": own_tenant.slug})

        self.assertIsNone(connect_result)
        joined_rooms = {item.args[0] for item in join_room.call_args_list}
        self.assertIn("municipio_910", joined_rooms)
        self.assertIn(f"tenant_{own_tenant.id}", joined_rooms)
        subscription = next(
            item for item in emit.call_args_list if item.args[0] == "subscribed_ticket_updates"
        )
        self.assertIn(f"tenant_{own_tenant.id}", subscription.args[1]["rooms"])

    def test_http_only_cookie_authenticates_operator_socket_message(self):
        token = jwt.encode(
            {"user_id": self.admin.id},
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        cookie_request = SimpleNamespace(
            sid="cookie-operator-message",
            cookies={self.app.config["AUTH_TOKEN_COOKIE_NAME"]: token},
        )

        with patch("socket_service.request", cookie_request), patch(
            "socket_service.servicio_tickets.crear_comentario", return_value=None
        ) as create_comment:
            handle_send_chat_message(
                {
                    "room": build_ticket_room("municipio", self.ticket.id),
                    "ticket_id": self.ticket.id,
                    "ticket_type": "municipio",
                    "message": "Respuesta autenticada por cookie",
                }
            )

        create_comment.assert_called_once()

    def test_terminal_clerk_event_disconnects_each_bound_socket_once(self):
        participants = {
            "clerk_session:sess_disconnect": [("socket-one", "engine-one"), ("socket-two", "engine-two")],
            "clerk_user:user_disconnect": [("socket-one", "engine-one")],
        }

        with patch.object(
            socketio.server.manager,
            "get_participants",
            side_effect=lambda namespace, room: participants.get(room, []),
        ), patch.object(socketio.server, "disconnect") as disconnect:
            count = disconnect_clerk_session_sockets(
                clerk_session_id="sess_disconnect",
                clerk_user_id="user_disconnect",
            )

        self.assertEqual(count, 2)
        self.assertEqual({item.args[0] for item in disconnect.call_args_list}, {"socket-one", "socket-two"})
        self.assertTrue(all(item.kwargs == {"namespace": "/"} for item in disconnect.call_args_list))

    def test_location_requires_previously_authorized_socket_room(self):
        with patch(
            "socket_service.request", SimpleNamespace(sid="unscoped-location")
        ), patch.object(
            socketio.server, "rooms",
            return_value=["unscoped-location", "encuesta:junin:consulta"],
        ), patch("services.municipio_responder.handle_location_update") as handle_location, patch(
            "socket_service.socketio.emit"
        ) as socket_emit, patch("socket_service.emit") as emit:
            on_location({"lat": -34.6, "lon": -58.4})

        handle_location.assert_not_called()
        socket_emit.assert_not_called()
        emit.assert_called_once_with("location_error", {"error": "authorized_room_required"})

    def test_location_emits_only_to_joined_high_entropy_session_room(self):
        room = "e193f1d7-261d-43b7-a2a8-54b62e559f45"
        response = {"respuesta": "Ubicacion actualizada"}
        with patch(
            "socket_service.request", SimpleNamespace(sid="scoped-location")
        ), patch.object(
            socketio.server, "rooms",
            return_value=["scoped-location", room],
        ), patch(
            "services.municipio_responder.handle_location_update",
            return_value=response,
        ) as handle_location, patch("socket_service.socketio.emit") as socket_emit:
            on_location({"lat": -34.6, "lon": -58.4, "room": room})

        handle_location.assert_called_once()
        socket_emit.assert_called_once_with("message", response, room=room)

    def test_unsigned_ticket_room_join_is_rejected(self):
        room = build_ticket_room("municipio", self.ticket.id)

        with patch("socket_service.join_room") as join_room, patch("socket_service.emit") as emit:
            on_join({"room": room})

        join_room.assert_not_called()
        emit.assert_called_once_with(
            "join_error",
            {"error": "missing_access_token", "room": room},
        )

    def test_signed_token_only_joins_its_open_ticket_room(self):
        room = build_ticket_room("municipio", self.ticket.id)
        token = issue_ticket_room_token("municipio", self.ticket.id)

        with patch("socket_service.join_room") as join_room, patch("socket_service.emit") as emit:
            on_join({"room": room, "access_token": token})

        join_room.assert_called_once_with(room)
        emit.assert_called_once_with(
            "join_ack",
            {"room": room, "access_mode": "signed_ticket_room"},
        )

    def test_signed_token_cannot_join_another_ticket_room(self):
        other_ticket = MunicipioTicket(
            municipio_id=910,
            pregunta="Otro ciudadano",
            estado="esperando_agente_en_vivo",
            nro_ticket="LIVE-911",
            anon_id="visitor-911",
        )
        db.session.add(other_ticket)
        db.session.commit()
        token = issue_ticket_room_token("municipio", self.ticket.id)
        other_room = build_ticket_room("municipio", other_ticket.id)

        with patch("socket_service.join_room") as join_room, patch("socket_service.emit") as emit:
            on_join({"room": other_room, "access_token": token})

        join_room.assert_not_called()
        self.assertEqual(emit.call_args.args[0], "join_error")
        self.assertEqual(emit.call_args.args[1]["error"], "ticket_room_mismatch")

    def test_signed_token_missing_expiry_is_rejected(self):
        room = build_ticket_room("municipio", self.ticket.id)
        token = jwt.encode(
            {
                "iss": LIVE_CHAT_TOKEN_ISSUER,
                "aud": LIVE_CHAT_TOKEN_AUDIENCE,
                "scope": LIVE_CHAT_TOKEN_SCOPE,
                "ticket_type": "municipio",
                "ticket_id": self.ticket.id,
                "room": room,
            },
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )

        with patch("socket_service.join_room") as join_room, patch("socket_service.emit") as emit:
            on_join({"room": room, "access_token": token})

        join_room.assert_not_called()
        emit.assert_called_once_with(
            "join_error",
            {"error": "invalid_access_token", "room": room},
        )

    def test_tenant_scoped_public_survey_room_remains_joinable(self):
        tenant = TenantProfile(
            slug="junin",
            nombre="Junin",
            tipo="municipio",
            plan="full",
            municipio_id=self.admin.id,
        )
        db.session.add(tenant)
        db.session.flush()
        survey = EncEncuesta(
            tenant_id=tenant.id,
            slug="consulta-barrial",
            titulo="Consulta barrial",
            estado="publicada",
        )
        db.session.add(survey)
        db.session.flush()
        db.session.add(EncLink(encuesta_id=survey.id, slug_publico="consulta-barrial", canal="web"))
        db.session.commit()

        with patch("socket_service.join_room") as join_room, patch("socket_service.emit") as emit:
            on_join({"room": "encuesta:junin:consulta-barrial"})

        join_room.assert_called_once_with("encuesta:junin:consulta-barrial")
        emit.assert_not_called()

    def test_legacy_unscoped_survey_room_is_rejected(self):
        with patch("socket_service.join_room") as join_room, patch("socket_service.emit") as emit:
            on_join({"room": "encuesta_consulta-barrial"})

        join_room.assert_not_called()
        emit.assert_called_once_with(
            "join_error",
            {"error": "room_not_joinable", "room": "encuesta_consulta-barrial"},
        )

    def test_anonymous_tenant_room_join_is_rejected(self):
        with patch("socket_service.join_room") as join_room, patch("socket_service.emit") as emit:
            on_join({"room": "municipio_910", "channel": "web"})

        join_room.assert_not_called()
        emit.assert_called_once_with(
            "join_error",
            {"error": "room_not_joinable", "room": "municipio_910"},
        )

    def test_high_entropy_web_session_room_remains_joinable(self):
        room = "e193f1d7-261d-43b7-a2a8-54b62e559f45"
        db.session.add(ChatSessionContext(chat_session_id=room, context_data={}))
        db.session.commit()
        with patch("socket_service.join_room") as join_room, patch("socket_service.emit") as emit:
            on_join({"room": room, "channel": "web"})

        join_room.assert_called_once_with(room)
        emit.assert_not_called()

    def test_unpersisted_web_session_room_is_rejected(self):
        room = "bd4f90bf-dc81-4ad0-af64-aa70c08472bf"
        with patch("socket_service.join_room") as join_room, patch("socket_service.emit") as emit:
            on_join({"room": room, "channel": "web"})

        join_room.assert_not_called()
        emit.assert_called_once_with(
            "join_error",
            {"error": "room_not_joinable", "room": room},
        )

    def test_closed_ticket_room_cannot_be_rejoined(self):
        room = build_ticket_room("municipio", self.ticket.id)
        token = issue_ticket_room_token("municipio", self.ticket.id)
        self.ticket.estado = "cerrado"
        db.session.commit()

        with patch("socket_service.join_room") as join_room, patch("socket_service.emit") as emit:
            on_join({"room": room, "access_token": token})

        join_room.assert_not_called()
        emit.assert_called_once_with(
            "join_error",
            {"error": "ticket_closed", "room": room},
        )

    def test_operator_socket_message_is_scoped_to_its_ticket(self):
        token = jwt.encode(
            {"user_id": self.admin.id},
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )

        with patch("socket_service.servicio_tickets.crear_comentario", return_value=None) as create_comment:
            handle_send_chat_message(
                {
                    "token": token,
                    "room": "municipio_910",
                    "ticket_id": self.ticket.id,
                    "ticket_type": "municipio",
                    "message": "Respuesta del operador",
                }
            )

        create_comment.assert_called_once()

    def test_operator_socket_accepts_exact_utf8_reply_limit(self):
        token = jwt.encode(
            {"user_id": self.admin.id},
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        message = "á" * (OMNICHANNEL_REPLY_MAX_BODY_BYTES // 2)
        self.assertEqual(len(message.encode("utf-8")), OMNICHANNEL_REPLY_MAX_BODY_BYTES)

        with patch(
            "socket_service.servicio_tickets.crear_comentario",
            return_value=None,
        ) as create_comment, patch("socket_service.emit") as emit:
            handle_send_chat_message(
                {
                    "token": token,
                    "room": build_ticket_room("municipio", self.ticket.id),
                    "ticket_id": self.ticket.id,
                    "ticket_type": "municipio",
                    "message": message,
                }
            )

        create_comment.assert_called_once()
        self.assertEqual(
            create_comment.call_args.kwargs["comentario_data"]["comentario"],
            message,
        )
        emit.assert_not_called()

    def test_operator_socket_rejects_multibyte_reply_over_limit_without_side_effects(self):
        token = jwt.encode(
            {"user_id": self.admin.id},
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        message = "á" * ((OMNICHANNEL_REPLY_MAX_BODY_BYTES // 2) + 1)
        before = TicketComentario.query.filter_by(municipio_ticket_id=self.ticket.id).count()

        with patch(
            "socket_service.servicio_tickets.crear_comentario"
        ) as create_comment, patch("socket_service.socketio.emit") as socket_emit, patch(
            "socket_service.emit"
        ) as emit:
            handle_send_chat_message(
                {
                    "token": token,
                    "room": build_ticket_room("municipio", self.ticket.id),
                    "ticket_id": self.ticket.id,
                    "ticket_type": "municipio",
                    "message": message,
                }
            )

        create_comment.assert_not_called()
        socket_emit.assert_not_called()
        emit.assert_called_once_with("chat_error", {"error": "reply_body_too_large"})
        self.assertEqual(
            TicketComentario.query.filter_by(municipio_ticket_id=self.ticket.id).count(),
            before,
        )

    def test_category_restricted_employee_cannot_write_ticket_by_id(self):
        tenant = TenantProfile(
            slug="socket-category-write-scope",
            nombre="Socket category write scope",
            tipo="municipio",
            municipio_id=910,
        )
        db.session.add(tenant)
        db.session.flush()
        self.ticket.tenant_id = tenant.id
        self.ticket.categoria = "salud"
        employee = User(
            name="Lighting employee",
            email="socket-lighting-employee@example.com",
            rol="empleado",
            es_empleado=True,
            tipo_chat="municipio",
            municipio_id=910,
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
            accesibilidad={"employee_scope": {"categorias": ["alumbrado"]}},
        )
        employee.set_password("pass")
        db.session.add(employee)
        db.session.commit()
        token = jwt.encode(
            {"user_id": employee.id, "tenant_id": tenant.id},
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        before = TicketComentario.query.filter_by(municipio_ticket_id=self.ticket.id).count()

        with patch(
            "socket_service.servicio_tickets.crear_comentario"
        ) as create_comment, patch("socket_service.socketio.emit") as socket_emit, patch(
            "socket_service.emit"
        ) as emit:
            handle_send_chat_message(
                {
                    "token": token,
                    "room": build_ticket_room("municipio", self.ticket.id),
                    "ticket_id": self.ticket.id,
                    "ticket_type": "municipio",
                    "message": "No debe persistirse",
                }
            )

        create_comment.assert_not_called()
        socket_emit.assert_not_called()
        emit.assert_called_once_with("chat_error", {"error": "ticket_not_found"})
        self.assertEqual(
            TicketComentario.query.filter_by(municipio_ticket_id=self.ticket.id).count(),
            before,
        )

    def test_operator_socket_message_exposes_only_sanitized_public_payload(self):
        token = jwt.encode(
            {"user_id": self.admin.id},
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        stored_comment = SimpleNamespace(
            to_dict=lambda: {
                "id": 551,
                "comentario": "Respuesta segura",
                "texto": "Respuesta segura",
                "origen": "chat",
                "es_admin": True,
                "user_id": self.admin.id,
                "anon_id": "private-contact-id",
                "actor_identity": {
                    "email": self.admin.email,
                    "phone": "+5491111111111",
                    "user_id": self.admin.id,
                },
            }
        )

        with patch(
            "socket_service.servicio_tickets.crear_comentario",
            return_value=stored_comment,
        ), patch("socket_service.socketio.emit") as socket_emit, patch(
            "services.email_service.enviar_email_ticket_novedad"
        ) as direct_email, patch(
            "services.email_service.enviar_sms_ticket_novedad"
        ) as direct_sms, patch(
            "services.email_service.enviar_whatsapp_ticket_novedad"
        ) as direct_whatsapp:
            handle_send_chat_message(
                {
                    "token": token,
                    "room": build_ticket_room("municipio", self.ticket.id),
                    "ticket_id": self.ticket.id,
                    "ticket_type": "municipio",
                    "message": "Respuesta segura",
                }
            )

        direct_email.assert_not_called()
        direct_sms.assert_not_called()
        direct_whatsapp.assert_not_called()

        public_room = build_ticket_room("municipio", self.ticket.id)
        public_events = [
            item
            for item in socket_emit.call_args_list
            if item.kwargs.get("room") == public_room
        ]
        self.assertGreaterEqual(len(public_events), 1)
        for item in public_events:
            public_payload = item.args[1]
            serialized = str(public_payload)
            self.assertNotIn(self.admin.email, serialized)
            self.assertNotIn("+5491111111111", serialized)
            self.assertNotIn("private-contact-id", serialized)
            self.assertNotIn("actor_identity", serialized)
        public_chat = next(item for item in public_events if item.args[0] == "new_chat_message")
        self.assertNotIn("user_id", public_chat.args[1]["message"])
        self.assertNotIn("anon_id", public_chat.args[1]["message"])

    def test_pyme_socket_reply_stages_whatsapp_without_direct_provider_send(self):
        rubro = Rubro(clave="socket-pyme-outbox", nombre="Socket PyME Outbox")
        pyme_admin = User(
            name="PyME Socket Admin",
            email="pyme-socket-admin@example.com",
            rol="admin",
            tipo_chat="pyme",
        )
        pyme_admin.set_password("pass")
        db.session.add_all([rubro, pyme_admin])
        db.session.flush()
        pyme_admin.rubro_id = rubro.id
        tenant = TenantProfile(
            slug="socket-pyme-outbox",
            nombre="Socket PyME Outbox",
            tipo="pyme",
            pyme_id=pyme_admin.id,
            is_active=True,
        )
        db.session.add(tenant)
        db.session.flush()
        pyme_admin.tenant_id = tenant.id
        ticket = PymeTicket(
            tenant_id=tenant.id,
            pregunta="Consulta por entrega",
            estado="esperando_agente_en_vivo",
            nro_ticket=919191,
            rubro_id=rubro.id,
            email="pyme-socket-requester@example.com",
            telefono="+5492613555555",
        )
        db.session.add(ticket)
        db.session.commit()

        self.app.config.update(
            DOMAIN_EFFECT_OUTBOX_MODE="queue",
            DOMAIN_EFFECT_OUTBOX_SECRET="socket-comment-outbox-" + ("x" * 32),
            DOMAIN_EFFECT_OUTBOX_TENANT_IDS=str(tenant.id),
            DOMAIN_EFFECT_OUTBOX_MAX_PAYLOAD_BYTES=4096,
            DOMAIN_EFFECT_OUTBOX_MAX_ATTEMPTS=8,
            ENABLE_PYME_WHATSAPP_CHAT=True,
        )
        token = jwt.encode(
            {"user_id": pyme_admin.id},
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )

        with patch(
            "services.email_service.enviar_email_ticket_novedad"
        ) as direct_email, patch(
            "services.email_service.enviar_sms_ticket_novedad"
        ) as direct_sms, patch(
            "services.email_service.enviar_whatsapp_ticket_novedad"
        ) as direct_whatsapp, patch(
            "services.domain_effect_worker.enqueue_domain_effect_dispatch",
            return_value=True,
        ) as enqueue_dispatch, patch(
            "socket_service.socketio.emit"
        ):
            handle_send_chat_message(
                {
                    "token": token,
                    "room": build_ticket_room("pyme", ticket.id),
                    "ticket_id": ticket.id,
                    "ticket_type": "pyme",
                    "message": "Tu pedido sale hoy",
                }
            )

        direct_email.assert_not_called()
        direct_sms.assert_not_called()
        direct_whatsapp.assert_not_called()
        enqueue_dispatch.assert_called_once_with(tenant_id=tenant.id)
        rows = DomainEffectOutbox.query.filter_by(
            tenant_id=tenant.id,
            aggregate_type=PYME_COMMENT_AGGREGATE,
        ).all()
        self.assertEqual(
            [row.handler_name for row in rows],
            [
                "ticket.comment.email.requester.v1",
                "ticket.comment.sms.requester.v1",
                COMMENT_REQUESTER_WHATSAPP_HANDLER,
            ],
        )
        self.assertEqual(rows[-1].status, DomainEffectOutbox.STATUS_PENDING)

    def test_ticket_status_event_reaches_signed_room_with_sanitized_payload(self):
        room = build_ticket_room("municipio", self.ticket.id)
        event = {
            "tenant_type": "municipio",
            "tipo": "municipio",
            "municipio_id": 910,
            "ticket_id": self.ticket.id,
            "estado": "cerrado",
            "previous_status": "en_proceso",
            "changed_at": "2026-07-11T12:00:00+00:00",
            "internal_note": "never public",
        }

        with patch("socket_service.socketio.emit") as socket_emit:
            emit_ticket_status_changed(event)

        public_status = next(
            item
            for item in socket_emit.call_args_list
            if item.args[0] == "ticket.status.changed" and item.kwargs.get("room") == room
        )
        self.assertEqual(public_status.args[1]["estado"], "cerrado")
        self.assertEqual(public_status.args[1]["previous_status"], "en_proceso")
        self.assertNotIn("internal_note", public_status.args[1])
        self.assertNotIn("municipio_id", public_status.args[1])

    def test_ticket_assignment_event_reaches_signed_room_without_agent_identity(self):
        room = build_ticket_room("municipio", self.ticket.id)
        event = {
            "tenant_type": "municipio",
            "tipo": "municipio",
            "municipio_id": 910,
            "ticket_id": self.ticket.id,
            "estado": "en_proceso",
            "assignment_state": "assigned",
            "assignee_name": "Private Agent",
            "assignee_email": "private-agent@example.com",
        }

        with patch("socket_service.socketio.emit") as socket_emit:
            emit_ticket_assignment_changed(event)

        public_assignment = next(
            item
            for item in socket_emit.call_args_list
            if item.args[0] == "ticket.assignment.changed" and item.kwargs.get("room") == room
        )
        self.assertEqual(public_assignment.args[1]["assignment_state"], "assigned")
        self.assertNotIn("assignee_name", public_assignment.args[1])
        self.assertNotIn("assignee_email", public_assignment.args[1])

    def test_widget_cannot_write_foreign_ticket_without_signed_ticket_token(self):
        self.admin.entity_token = "attacker-tenant-entity-token"
        foreign_ticket = MunicipioTicket(
            municipio_id=999,
            pregunta="Ticket privado de otro municipio",
            estado="esperando_agente_en_vivo",
            nro_ticket="LIVE-FOREIGN-999",
            anon_id="foreign-visitor",
        )
        db.session.add(foreign_ticket)
        db.session.commit()

        response = self.client.post(
            "/api/ask/municipio",
            json={
                "pregunta": "Intento de escritura cruzada",
                "tipo_chat": "municipio",
                "ticket_id": foreign_ticket.id,
                "tipo_ticket": "municipio",
            },
            headers={
                "Origin": "https://www.chatboc.ar",
                "X-Entity-Token": self.admin.entity_token,
                "X-Chat-Session-Id": "cross-tenant-live-chat-attempt",
                "X-Anon-Id": "attacker-visitor",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            TicketComentario.query.filter_by(municipio_ticket_id=foreign_ticket.id).count(),
            0,
        )

    def test_widget_signed_ticket_token_persists_and_emits_sanitized_message(self):
        foreign_ticket = MunicipioTicket(
            municipio_id=999,
            pregunta="Ticket con acceso firmado",
            estado="esperando_agente_en_vivo",
            nro_ticket="LIVE-SIGNED-999",
            anon_id="signed-visitor",
        )
        db.session.add(foreign_ticket)
        db.session.commit()
        access_token = issue_ticket_room_token("municipio", foreign_ticket.id)

        with patch("services.email_service.enviar_email_ticket_admin"), patch(
            "socket_service.socketio.emit"
        ) as socket_emit:
            response = self.client.post(
                "/api/ask/municipio",
                json={
                    "pregunta": "Mensaje autorizado",
                    "tipo_chat": "municipio",
                    "ticket_id": foreign_ticket.id,
                    "tipo_ticket": "municipio",
                    "live_chat_access_token": access_token,
                },
                headers={
                    "Origin": "https://www.chatboc.ar",
                    "X-Chat-Session-Id": "signed-live-chat-message",
                    "X-Anon-Id": "signed-visitor",
                },
            )

        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json().get("status"), "message_sent_to_live_chat")
        self.assertEqual(
            TicketComentario.query.filter_by(municipio_ticket_id=foreign_ticket.id).count(),
            1,
        )
        public_room = build_ticket_room("municipio", foreign_ticket.id)
        public_chat = next(
            item
            for item in socket_emit.call_args_list
            if item.args[0] == "new_chat_message" and item.kwargs.get("room") == public_room
        )
        serialized = str(public_chat.args[1])
        self.assertNotIn("actor_identity", serialized)
        self.assertNotIn("anon_id", serialized)
        self.assertNotIn("user_id", public_chat.args[1]["message"])

    def test_operator_cannot_send_to_another_municipality_ticket(self):
        foreign_admin = User(
            name="Foreign admin",
            email="foreign-socket-admin@example.com",
            rol="admin",
            tipo_chat="municipio",
            municipio_id=999,
        )
        foreign_admin.set_password("pass")
        db.session.add(foreign_admin)
        db.session.commit()
        token = jwt.encode(
            {"user_id": foreign_admin.id},
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )

        with patch("socket_service.servicio_tickets.crear_comentario") as create_comment, patch(
            "socket_service.emit"
        ) as emit:
            handle_send_chat_message(
                {
                    "token": token,
                    "room": build_ticket_room("municipio", self.ticket.id),
                    "ticket_id": self.ticket.id,
                    "ticket_type": "municipio",
                    "message": "Intento cruzado",
                }
            )

        create_comment.assert_not_called()
        emit.assert_called_once_with("chat_error", {"error": "ticket_forbidden"})

    def test_legacy_new_chat_cannot_relay_arbitrary_room_payloads(self):
        with patch("socket_service.socketio.emit") as socket_emit, patch("socket_service.emit") as emit:
            on_new_chat({"room": "municipio_910", "message": "Mensaje falso"})

        socket_emit.assert_not_called()
        emit.assert_called_once_with("chat_error", {"error": "event_not_supported"})

    def test_authenticated_user_cannot_subscribe_to_foreign_tenant_slug(self):
        foreign_tenant = TenantProfile(
            slug="foreign-municipality",
            nombre="Foreign municipality",
            tipo="municipio",
            municipio_id=999,
        )
        db.session.add(foreign_tenant)
        db.session.commit()
        token = jwt.encode(
            {"user_id": self.admin.id},
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )

        with patch("socket_service.join_room") as join_room, patch("socket_service.emit") as emit:
            on_subscribe_ticket_updates(
                {"token": token, "tenant_slug": foreign_tenant.slug}
            )

        join_room.assert_not_called()
        emit.assert_called_once_with("subscription_error", {"error": "tenant_forbidden"})

    def test_authenticated_user_keeps_own_tenant_subscription(self):
        own_tenant = TenantProfile(
            slug="own-municipality",
            nombre="Own municipality",
            tipo="municipio",
            municipio_id=910,
        )
        db.session.add(own_tenant)
        db.session.commit()
        token = jwt.encode(
            {"user_id": self.admin.id},
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )

        with patch("socket_service.join_room") as join_room, patch("socket_service.emit") as emit:
            on_subscribe_ticket_updates(
                {"token": token, "tenant_slug": own_tenant.slug}
            )

        joined_rooms = {call.args[0] for call in join_room.call_args_list}
        self.assertIn("municipio_910", joined_rooms)
        self.assertIn(f"tenant_{own_tenant.id}", joined_rooms)
        self.assertEqual(emit.call_args.args[0], "subscribed_ticket_updates")
        self.assertEqual(set(emit.call_args.args[1]["rooms"]), joined_rooms)

    def test_end_user_cannot_subscribe_to_same_tenant_operator_rooms(self):
        tenant = TenantProfile(
            slug="customer-municipality",
            nombre="Customer municipality",
            tipo="municipio",
            municipio_id=910,
        )
        db.session.add(tenant)
        db.session.flush()
        customer = User(
            name="Citizen",
            email="citizen-socket@example.com",
            rol="usuario",
            tipo_chat="municipio",
            tenant_id=tenant.id,
        )
        customer.set_password("pass")
        db.session.add(customer)
        db.session.commit()
        token = jwt.encode(
            {"user_id": customer.id},
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )

        with patch("socket_service.join_room") as join_room, patch("socket_service.emit") as emit:
            on_subscribe_ticket_updates({"token": token, "tenant_slug": tenant.slug})

        join_room.assert_not_called()
        emit.assert_called_once_with("subscription_error", {"error": "tenant_forbidden"})

    def test_shared_rubro_does_not_allow_cross_tenant_pyme_write(self):
        rubro = Rubro(clave="shared-hardware", nombre="Ferreteria")
        db.session.add(rubro)
        db.session.flush()
        first_admin = User(
            name="First business",
            email="first-business@example.com",
            rol="admin",
            tipo_chat="pyme",
            rubro_id=rubro.id,
        )
        second_admin = User(
            name="Second business",
            email="second-business@example.com",
            rol="admin",
            tipo_chat="pyme",
            rubro_id=rubro.id,
        )
        first_admin.set_password("pass")
        second_admin.set_password("pass")
        db.session.add_all([first_admin, second_admin])
        db.session.flush()
        first_tenant = TenantProfile(
            slug="first-business",
            nombre="First business",
            tipo="pyme",
            pyme_id=first_admin.id,
        )
        second_tenant = TenantProfile(
            slug="second-business",
            nombre="Second business",
            tipo="pyme",
            pyme_id=second_admin.id,
        )
        db.session.add_all([first_tenant, second_tenant])
        db.session.flush()
        first_admin.tenant_id = first_tenant.id
        second_admin.tenant_id = second_tenant.id
        foreign_ticket = PymeTicket(
            tenant_id=second_tenant.id,
            pregunta="Pedido privado",
            estado="esperando_agente_en_vivo",
            nro_ticket=912,
            user_id=second_admin.id,
            rubro_id=rubro.id,
        )
        db.session.add(foreign_ticket)
        db.session.commit()
        token = jwt.encode(
            {"user_id": first_admin.id},
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )

        with patch("socket_service.servicio_tickets.crear_comentario") as create_comment, patch(
            "socket_service.emit"
        ) as emit:
            handle_send_chat_message(
                {
                    "token": token,
                    "room": build_ticket_room("pyme", foreign_ticket.id),
                    "ticket_id": foreign_ticket.id,
                    "ticket_type": "pyme",
                    "message": "Intento entre empresas del mismo rubro",
                }
            )

        create_comment.assert_not_called()
        emit.assert_called_once_with("chat_error", {"error": "ticket_forbidden"})

    def test_whatsapp_live_chat_lookup_uses_exact_pyme_tenant_not_shared_rubro(self):
        rubro = Rubro(clave="shared-whatsapp-hardware", nombre="Ferreteria")
        db.session.add(rubro)
        db.session.flush()
        first_owner = User(
            name="First WhatsApp business",
            email="first-whatsapp-business@example.com",
            rol="admin",
            tipo_chat="pyme",
            rubro_id=rubro.id,
        )
        second_owner = User(
            name="Second WhatsApp business",
            email="second-whatsapp-business@example.com",
            rol="admin",
            tipo_chat="pyme",
            rubro_id=rubro.id,
        )
        customer = User(
            name="Shared customer",
            email="shared-whatsapp-customer@example.com",
            rol="usuario",
        )
        first_owner.set_password("pass")
        second_owner.set_password("pass")
        customer.set_password("pass")
        db.session.add_all([first_owner, second_owner, customer])
        db.session.flush()
        first_tenant = TenantProfile(
            slug="first-whatsapp-business",
            nombre="First WhatsApp business",
            tipo="pyme",
            pyme_id=first_owner.id,
        )
        second_tenant = TenantProfile(
            slug="second-whatsapp-business",
            nombre="Second WhatsApp business",
            tipo="pyme",
            pyme_id=second_owner.id,
        )
        db.session.add_all([first_tenant, second_tenant])
        db.session.flush()
        first_owner.tenant_id = first_tenant.id
        second_owner.tenant_id = second_tenant.id
        first_ticket = PymeTicket(
            tenant_id=first_tenant.id,
            pregunta="Pedido del primer negocio",
            estado="esperando_agente_en_vivo",
            nro_ticket=921,
            user_id=customer.id,
            anon_id="+5491111111111",
            rubro_id=rubro.id,
            fecha=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        foreign_newer_ticket = PymeTicket(
            tenant_id=second_tenant.id,
            pregunta="Pedido privado del segundo negocio",
            estado="esperando_agente_en_vivo",
            nro_ticket=922,
            user_id=customer.id,
            anon_id="+5491111111111",
            rubro_id=rubro.id,
            fecha=datetime.now(timezone.utc),
        )
        db.session.add_all([first_ticket, foreign_newer_ticket])
        db.session.commit()

        ticket_type, resolved_ticket = _find_live_chat_ticket(
            first_owner,
            customer,
            "+5491111111111",
            first_tenant,
            {
                "human_chat_in_progress": True,
                "ticket_id": first_ticket.id,
                "tipo_ticket": "pyme",
                "room": f"ticket_pyme_{first_ticket.id}",
            },
        )

        self.assertEqual(ticket_type, "pyme")
        self.assertEqual(resolved_ticket.id, first_ticket.id)
        self.assertEqual(resolved_ticket.tenant_id, first_tenant.id)

    def test_whatsapp_live_chat_uses_persisted_ticket_when_contact_has_two_active_cases(self):
        owner = User(
            name="Exact handoff business",
            email="exact-handoff-business@example.com",
            rol="admin",
            tipo_chat="pyme",
        )
        customer = User(
            name="Two active cases customer",
            email="two-active-cases@example.com",
            rol="usuario",
        )
        owner.set_password("pass")
        customer.set_password("pass")
        db.session.add_all([owner, customer])
        db.session.flush()
        tenant = TenantProfile(
            slug="exact-handoff-business",
            nombre="Exact handoff business",
            tipo="pyme",
            pyme_id=owner.id,
        )
        db.session.add(tenant)
        db.session.flush()
        owner.tenant_id = tenant.id
        persisted_ticket = PymeTicket(
            tenant_id=tenant.id,
            pregunta="Caso que originó el handoff",
            estado="esperando_agente_en_vivo",
            nro_ticket=931,
            user_id=customer.id,
            anon_id="+5491111111111",
            fecha=datetime.now(timezone.utc) - timedelta(minutes=10),
        )
        newer_ticket = PymeTicket(
            tenant_id=tenant.id,
            pregunta="Segundo caso activo más reciente",
            estado="esperando_agente_en_vivo",
            nro_ticket=932,
            user_id=customer.id,
            anon_id="+5491111111111",
            fecha=datetime.now(timezone.utc),
        )
        db.session.add_all([persisted_ticket, newer_ticket])
        db.session.commit()

        ticket_type, resolved_ticket = _find_live_chat_ticket(
            owner,
            customer,
            "+5491111111111",
            tenant,
            {
                "human_chat_in_progress": True,
                "ticket_id": persisted_ticket.id,
                "tipo_ticket": "pyme",
                "room": f"ticket_pyme_{persisted_ticket.id}",
            },
        )
        _unbound_type, unbound_ticket = _find_live_chat_ticket(
            owner,
            customer,
            "+5491111111111",
            tenant,
            {"human_chat_in_progress": True, "room": f"ticket_pyme_{newer_ticket.id}"},
        )

        self.assertEqual(ticket_type, "pyme")
        self.assertEqual(resolved_ticket.id, persisted_ticket.id)
        self.assertNotEqual(resolved_ticket.id, newer_ticket.id)
        self.assertIsNone(unbound_ticket)

    def test_whatsapp_live_chat_context_cannot_select_another_contacts_ticket(self):
        owner = User(
            name="Contact scoped business",
            email="contact-scoped-business@example.com",
            rol="admin",
            tipo_chat="pyme",
        )
        customer = User(
            name="Expected contact",
            email="expected-contact@example.com",
            rol="usuario",
        )
        other_customer = User(
            name="Other contact",
            email="other-contact@example.com",
            rol="usuario",
        )
        owner.set_password("pass")
        customer.set_password("pass")
        other_customer.set_password("pass")
        db.session.add_all([owner, customer, other_customer])
        db.session.flush()
        tenant = TenantProfile(
            slug="contact-scoped-business",
            nombre="Contact scoped business",
            tipo="pyme",
            pyme_id=owner.id,
        )
        db.session.add(tenant)
        db.session.flush()
        foreign_contact_ticket = PymeTicket(
            tenant_id=tenant.id,
            pregunta="Caso privado de otro contacto",
            estado="esperando_agente_en_vivo",
            nro_ticket=933,
            user_id=other_customer.id,
            anon_id="+5491222222222",
        )
        db.session.add(foreign_contact_ticket)
        db.session.commit()

        ticket_type, resolved_ticket = _find_live_chat_ticket(
            owner,
            customer,
            "+5491111111111",
            tenant,
            {
                "human_chat_in_progress": True,
                "ticket_id": foreign_contact_ticket.id,
                "tipo_ticket": "pyme",
            },
        )

        self.assertEqual(ticket_type, "pyme")
        self.assertIsNone(resolved_ticket)

    def test_pyme_comment_emitter_uses_exact_tenant_room_not_rubro_owner_room(self):
        first_owner = User(
            name="Scoped business",
            email="scoped-comment-business@example.com",
            rol="admin",
            tipo_chat="pyme",
        )
        foreign_owner = User(
            name="Foreign room business",
            email="foreign-room-business@example.com",
            rol="admin",
            tipo_chat="pyme",
        )
        first_owner.set_password("pass")
        foreign_owner.set_password("pass")
        db.session.add_all([first_owner, foreign_owner])
        db.session.flush()
        colliding_rubro = Rubro(
            id=foreign_owner.id,
            clave="colliding-room-rubro",
            nombre="Rubro con ID colisionado",
        )
        db.session.add(colliding_rubro)
        db.session.flush()
        first_owner.rubro_id = colliding_rubro.id
        foreign_owner.rubro_id = colliding_rubro.id
        first_tenant = TenantProfile(
            slug="scoped-comment-business",
            nombre="Scoped business",
            tipo="pyme",
            pyme_id=first_owner.id,
        )
        foreign_tenant = TenantProfile(
            slug="foreign-room-business",
            nombre="Foreign room business",
            tipo="pyme",
            pyme_id=foreign_owner.id,
        )
        db.session.add_all([first_tenant, foreign_tenant])
        db.session.flush()
        first_owner.tenant_id = first_tenant.id
        foreign_owner.tenant_id = foreign_tenant.id
        ticket = PymeTicket(
            tenant_id=first_tenant.id,
            pregunta="Consulta privada",
            estado="esperando_agente_en_vivo",
            nro_ticket=923,
            rubro_id=colliding_rubro.id,
        )
        db.session.add(ticket)
        db.session.commit()

        with patch("services.email_service.enviar_email_ticket_admin"), patch(
            "socket_service.socketio.emit"
        ) as socket_emit:
            comment = servicio_tickets.crear_comentario(
                ticket.id,
                "pyme",
                {
                    "comentario": "Mensaje del cliente",
                    "anon_id": "private-pyme-contact",
                    "es_admin": False,
                    "origen": "widget",
                },
            )

        self.assertIsNotNone(comment)
        emitted_rooms = {item.kwargs.get("room") for item in socket_emit.call_args_list}
        self.assertIn(f"tenant_{first_tenant.id}", emitted_rooms)
        self.assertIn(build_ticket_room("pyme", ticket.id), emitted_rooms)
        self.assertNotIn(f"pyme_{foreign_owner.id}", emitted_rooms)
        public_events = [
            item
            for item in socket_emit.call_args_list
            if item.kwargs.get("room") == build_ticket_room("pyme", ticket.id)
        ]
        self.assertTrue(public_events)
        for item in public_events:
            serialized = str(item.args[1])
            self.assertNotIn("private-pyme-contact", serialized)
            self.assertNotIn("actor_identity", serialized)

    def test_legacy_pyme_handoff_delegates_to_signed_ticket_contract(self):
        rubro = Rubro(clave="legacy-signed-handoff", nombre="Servicios")
        owner = User(
            name="Legacy handoff business",
            email="legacy-handoff-business@example.com",
            rol="admin",
            tipo_chat="pyme",
        )
        customer = User(
            name="Handoff customer",
            email="legacy-handoff-customer@example.com",
            rol="usuario",
        )
        owner.set_password("pass")
        customer.set_password("pass")
        db.session.add_all([rubro, owner, customer])
        db.session.flush()
        owner.rubro_id = rubro.id
        tenant = TenantProfile(
            slug="legacy-handoff-business",
            nombre="Legacy handoff business",
            tipo="pyme",
            pyme_id=owner.id,
        )
        db.session.add(tenant)
        db.session.flush()
        owner.tenant_id = tenant.id
        db.session.commit()
        context_data = {}
        handler = LegacyPymeHumanHandler(
            {
                CONTEXTO_PYME: {},
                "user_id": owner.id,
                "cliente_id": customer.id,
                "user_obj": owner,
                "viewer_user_obj": customer,
                "tenant_id": tenant.id,
                "tenant_profile": tenant,
                "chat_db_context_data": context_data,
                "anon_id": None,
            }
        )

        with patch.object(
            servicio_tickets,
            "_notificar_ticket_por_email",
        ), patch("services.email_service.enviar_email_ticket_admin"), patch(
            "services.actions.pyme_actions.emit_new_ticket"
        ), patch("socket_service.socketio.emit"):
            result = handler.execute({"pregunta": "Necesito hablar con una persona"})

        self.assertTrue(result.get("success"), result)
        data = result.get("data") or {}
        self.assertTrue(data.get("ticket_id"))
        self.assertEqual(data.get("socket_room"), build_ticket_room("pyme", data["ticket_id"]))
        self.assertTrue(data.get("live_chat_access_token"))
        verified = verify_ticket_room_token(
            data["live_chat_access_token"],
            expected_room=data["socket_room"],
        )
        self.assertEqual(verified["ticket_id"], data["ticket_id"])

    def test_existing_live_pyme_ticket_is_not_implicitly_persisted_without_signed_route(self):
        ticket = PymeTicket(
            pregunta="Chat ya derivado",
            estado="esperando_agente_en_vivo",
            nro_ticket=924,
        )
        db.session.add(ticket)
        db.session.commit()

        resolved = _resolve_pyme_chat_persistence_ticket_id(
            {"ultimo_ticket_creado": ticket.id},
            {},
        )

        self.assertIsNone(resolved)
        self.assertIsNone(
            _resolve_pyme_chat_persistence_ticket_id({}, {"ticket_id": ticket.id}),
        )

        regular_ticket = PymeTicket(
            pregunta="Consulta sin derivacion",
            estado="nuevo",
            nro_ticket=925,
        )
        db.session.add(regular_ticket)
        db.session.commit()
        self.assertEqual(
            _resolve_pyme_chat_persistence_ticket_id(
                {},
                {"ticket_id": regular_ticket.id},
            ),
            regular_ticket.id,
        )

    def test_unclear_fallback_calls_signed_handoff_execute_contract(self):
        context_data = {"initialized": True}
        handler = LegacyPymeUnclearHandler(
            {
                CONTEXTO_PYME: {"reintentos_ambigua": 2},
                "chat_db_context_data": context_data,
            }
        )
        signed_result = {
            "success": True,
            "data": {"live_chat_access_token": "signed-token"},
        }

        with patch.object(
            LegacyPymeHumanHandler,
            "execute",
            return_value=signed_result,
        ) as execute:
            result = handler.execute({"pregunta": "Necesito ayuda humana"})

        execute.assert_called_once_with({"pregunta": "Necesito ayuda humana"})
        self.assertEqual(result, signed_result)


if __name__ == "__main__":
    unittest.main()
