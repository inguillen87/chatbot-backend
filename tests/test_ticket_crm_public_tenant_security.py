from __future__ import annotations

import ast
import importlib.util
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from models import (
    CatalogoItem,
    ChatSessionContext,
    ClienteNota,
    Conversacion,
    LlmInteractionLog,
    MarketCart,
    MarketCartItem,
    MarketOrder,
    MunicipioTicket,
    PublicSurvey,
    PublicSurveyResponse,
    PymePedido,
    PymeTicket,
    SugerenciaCiudadano,
    TenantFollower,
    TenantProfile,
    TenantTicket,
    TicketComentario,
    User,
    WebAuthnCredential,
    db,
)
from routes.empleados import _empleados_query, _employee_comment_query
from services.user_merge import merge_anon_into_user


_LEGACY_CRM_PATH = Path(__file__).resolve().parents[1] / "routes" / "crm.py"
_CRM_SPEC = importlib.util.spec_from_file_location(
    "legacy_crm_routes_for_scope_tests",
    _LEGACY_CRM_PATH,
)
assert _CRM_SPEC is not None and _CRM_SPEC.loader is not None
_CRM_MODULE = importlib.util.module_from_spec(_CRM_SPEC)
_CRM_SPEC.loader.exec_module(_CRM_MODULE)
_crm_llm_log_query = _CRM_MODULE._crm_llm_log_query
_crm_note_query = _CRM_MODULE._crm_note_query


def test_ticket_panel_has_one_authoritative_route_definition():
    source = (Path(__file__).resolve().parents[1] / "routes" / "ticket.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    handlers = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "route"
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
            ):
                continue
            if decorator.args[0].value == "/tickets/panel":
                handlers.append(node.name)
    assert handlers == ["get_ticket_panel"]


def test_ticket_type_allowlist_rejects_every_dynamic_surface_before_lookup(client):
    owner = _user("ticket-type-owner@test.local", role="admin", tipo_chat="municipio")
    _tenant("ticket-type-tenant", owner, kind="municipio")
    db.session.commit()
    login = client.post(
        "/auth/login",
        json={"email": owner.email, "password": "test-pass"},
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.get_json()['token']}"}

    responses = [
        client.post(
            "/tickets/not-a-ticket/999/responder",
            headers=headers,
            json={"comentario": "must not be written"},
        ),
        client.put(
            "/tickets/not-a-ticket/999/estado",
            headers=headers,
            json={"estado": "cerrado"},
        ),
        client.post(
            "/tickets/not-a-ticket/999/ubicacion",
            headers=headers,
            json={"lat": -34.6, "lng": -58.4},
        ),
        client.post(
            "/tickets/not-a-ticket/999/encuesta",
            headers=headers,
            json={"puntuacion": 5},
        ),
        client.get("/tickets/not-a-ticket/999/encuesta", headers=headers),
        client.get("/tickets/not-a-ticket/mapa", headers=headers),
        client.get("/tickets/not-a-ticket/999/ruta", headers=headers),
        client.get("/tickets/not-a-ticket/999/timeline", headers=headers),
        client.post(
            "/tickets/not-a-ticket/999/presence",
            headers=headers,
            json={"presence_status": "active"},
        ),
        client.post(
            "/tickets/not-a-ticket/999/read-state",
            headers=headers,
            json={"last_read_comment_id": 1},
        ),
        client.post("/tickets/not-a-ticket/999/send-history", headers=headers),
    ]

    assert [response.status_code for response in responses] == [400] * len(responses)
    assert TicketComentario.query.count() == 0


def test_attachment_uploads_reject_cross_tenant_ticket_before_storage(client):
    owner_a = _user("upload-owner-a@test.local", role="admin", tipo_chat="municipio")
    owner_b = _user("upload-owner-b@test.local", role="admin", tipo_chat="municipio")
    tenant_a = _tenant("upload-a", owner_a, kind="municipio")
    tenant_b = _tenant("upload-b", owner_b, kind="municipio")
    ticket_b = MunicipioTicket(
        pregunta="tenant B only",
        municipio_id=owner_b.id,
        tenant_id=tenant_b.id,
    )
    db.session.add(ticket_b)
    db.session.commit()
    assert owner_a.tenant_id == tenant_a.id

    login = client.post(
        "/auth/login",
        json={"email": owner_a.email, "password": "test-pass"},
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.get_json()['token']}"}

    with patch("routes.archivos.upload_to_gcs") as upload_mock:
        response = client.post(
            "/archivos/subir",
            headers=headers,
            data={
                "archivo": (BytesIO(b"not-an-image"), "evidence.png"),
                "municipio_ticket_id": str(ticket_b.id),
            },
            content_type="multipart/form-data",
        )
    assert response.status_code == 404
    upload_mock.assert_not_called()

    with patch("routes.archivos.guardar_archivo_adjunto_ticket") as save_mock:
        response = client.post(
            "/archivos/subir_admin",
            headers=headers,
            data={
                "archivo": (BytesIO(b"not-an-image"), "evidence.png"),
                "ticket_id": str(ticket_b.id),
                "tipo_ticket": "municipio",
            },
            content_type="multipart/form-data",
        )
    assert response.status_code == 404
    save_mock.assert_not_called()


def _user(email: str, *, role: str = "usuario", **kwargs) -> User:
    user = User(email=email, name=email.split("@", 1)[0], rol=role, **kwargs)
    user.set_password("test-pass")
    db.session.add(user)
    db.session.flush()
    return user


def _tenant(slug: str, owner: User, *, kind: str = "pyme") -> TenantProfile:
    tenant = TenantProfile(
        slug=slug,
        nombre=slug,
        tipo=kind,
        plan="full",
        municipio_id=owner.id if kind == "municipio" else None,
        pyme_id=owner.id if kind != "municipio" else None,
    )
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id = tenant.id
    owner.tenant_slug = tenant.slug
    return tenant


def _all_keys(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key).lower()
            yield from _all_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _all_keys(nested)


def test_public_history_is_exact_user_tenant_and_session_scoped(client):
    owner_a = _user("owner-a@history.test", role="admin", tipo_chat="pyme")
    owner_b = _user("owner-b@history.test", role="admin", tipo_chat="pyme")
    tenant_a = _tenant("history-a", owner_a)
    tenant_b = _tenant("history-b", owner_b)
    viewer_a = _user(
        "viewer-a@history.test",
        anon_id="opaque-viewer-a",
        tenant_id=tenant_a.id,
        tenant_slug=tenant_a.slug,
    )
    other_a = _user(
        "other-a@history.test",
        anon_id="opaque-other-a",
        tenant_id=tenant_a.id,
        tenant_slug=tenant_a.slug,
    )
    viewer_b = _user(
        "viewer-b@history.test",
        anon_id="opaque-viewer-a",
        tenant_id=tenant_b.id,
        tenant_slug=tenant_b.slug,
    )

    db.session.add_all(
        [
            ChatSessionContext(
                chat_session_id="history-session-a",
                tenant_id=tenant_a.id,
                user_id=viewer_a.id,
            ),
            ChatSessionContext(
                chat_session_id="history-session-victim",
                tenant_id=tenant_a.id,
                user_id=other_a.id,
            ),
            ChatSessionContext(
                chat_session_id="history-session-b",
                tenant_id=tenant_b.id,
                user_id=viewer_b.id,
            ),
        ]
    )
    item_a = CatalogoItem(
        user_id=owner_a.id,
        tenant_id=tenant_a.id,
        nombre="History product A",
        categoria="general",
        precio="10",
        cantidad="10",
        disponible=True,
    )
    db.session.add(item_a)
    db.session.flush()
    cart_a = MarketCart(
        tenant_id=tenant_a.id,
        user_id=viewer_a.id,
        session_id="history-session-a",
        status="open",
    )
    cart_other = MarketCart(
        tenant_id=tenant_a.id,
        user_id=other_a.id,
        session_id="history-session-victim",
        status="open",
    )
    db.session.add_all([cart_a, cart_other])
    db.session.flush()
    db.session.add_all(
        [
            MarketCartItem(
                cart_id=cart_a.id,
                product_id=item_a.id,
                quantity=2,
                name_snapshot=item_a.nombre,
            ),
            MarketCartItem(
                cart_id=cart_other.id,
                product_id=item_a.id,
                quantity=9,
                name_snapshot=item_a.nombre,
            ),
            TenantTicket(
                tenant_id=tenant_a.id,
                user_id=viewer_a.id,
                categoria="own-tenant-claim",
                descripcion="owned by viewer A",
            ),
            TenantTicket(
                tenant_id=tenant_a.id,
                user_id=other_a.id,
                categoria="secret-other-tenant-claim",
                descripcion="must stay hidden",
            ),
            MunicipioTicket(
                pregunta="own municipal claim",
                asunto="own-municipal-claim",
                municipio_id=owner_a.id,
                tenant_id=tenant_a.id,
                user_id=viewer_a.id,
                consulta_pin="111111",
            ),
            MunicipioTicket(
                pregunta="other municipal claim",
                asunto="secret-other-municipal-claim",
                municipio_id=owner_a.id,
                tenant_id=tenant_a.id,
                user_id=other_a.id,
                consulta_pin="222222",
            ),
            MunicipioTicket(
                pregunta="tenant B claim",
                asunto="secret-tenant-b-claim",
                municipio_id=owner_b.id,
                tenant_id=tenant_b.id,
                user_id=viewer_b.id,
                consulta_pin="333333",
            ),
            PymePedido(
                pyme_id=owner_a.id,
                tenant_id=tenant_a.id,
                user_id=viewer_a.id,
                asunto="own-order",
                detalles="{}",
                monto_total=10,
            ),
            PymePedido(
                pyme_id=owner_a.id,
                tenant_id=tenant_a.id,
                user_id=other_a.id,
                asunto="secret-other-order",
                detalles="{}",
                monto_total=90,
            ),
            PymePedido(
                pyme_id=owner_b.id,
                tenant_id=tenant_b.id,
                user_id=viewer_b.id,
                asunto="secret-tenant-b-order",
                detalles="{}",
                monto_total=80,
            ),
            Conversacion(
                user_id=viewer_a.id,
                pyme_id=owner_a.id,
                pregunta="own-session-message",
                respuesta="own",
                fuente="widget",
                session_id="history-session-a",
            ),
            Conversacion(
                user_id=other_a.id,
                pyme_id=owner_a.id,
                pregunta="secret-victim-session-message",
                respuesta="secret",
                fuente="widget",
                session_id="history-session-victim",
            ),
        ]
    )
    db.session.commit()

    response = client.get(
        "/api/public/widget-user/tenant-history",
        query_string={"tenant_slug": tenant_a.slug},
        headers={
            "X-Anon-Id": "opaque-viewer-a",
            "X-Chat-Session-Id": "history-session-a",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    titles = {item["title"] for item in payload["items"]}
    assert payload["cart"]["items_count"] == 2
    assert {
        "own-tenant-claim",
        "own-municipal-claim",
        "own-order",
        "own-session-message",
    }.issubset(titles)
    assert not {
        "secret-other-tenant-claim",
        "secret-other-municipal-claim",
        "secret-tenant-b-claim",
        "secret-other-order",
        "secret-tenant-b-order",
        "secret-victim-session-message",
    }.intersection(titles)
    assert {"consulta_pin", "pin", "credential_transport"}.isdisjoint(
        set(_all_keys(payload))
    )

    forged_session = client.get(
        "/api/public/widget-user/tenant-history",
        query_string={"tenant_slug": tenant_a.slug},
        headers={
            "X-Anon-Id": "opaque-viewer-a",
            "X-Chat-Session-Id": "history-session-victim",
        },
    ).get_json()
    assert forged_session["cart"]["items_count"] == 0
    assert "secret-victim-session-message" not in {
        item["title"] for item in forged_session["items"]
    }
    assert "own-order" not in {item["title"] for item in forged_session["items"]}


def test_public_anonymous_history_never_treats_null_owner_as_wildcard(client):
    owner_a = _user("owner-anon-a@history.test", role="admin", tipo_chat="municipio")
    owner_b = _user("owner-anon-b@history.test", role="admin", tipo_chat="municipio")
    tenant_a = _tenant("anon-history-a", owner_a, kind="municipio")
    tenant_b = _tenant("anon-history-b", owner_b, kind="municipio")
    db.session.add_all(
        [
            MunicipioTicket(
                pregunta="exact anonymous",
                asunto="exact-anonymous-claim",
                municipio_id=owner_a.id,
                tenant_id=tenant_a.id,
                anon_id="opaque-pure-anon",
                consulta_pin="444444",
            ),
            MunicipioTicket(
                pregunta="legacy null",
                asunto="secret-null-owner-claim",
                municipio_id=owner_a.id,
                tenant_id=tenant_a.id,
                anon_id=None,
                consulta_pin="555555",
            ),
            MunicipioTicket(
                pregunta="same bearer other tenant",
                asunto="secret-other-tenant-anon-claim",
                municipio_id=owner_b.id,
                tenant_id=tenant_b.id,
                anon_id="opaque-pure-anon",
                consulta_pin="666666",
            ),
            PymePedido(
                pyme_id=owner_a.id,
                tenant_id=tenant_a.id,
                asunto="secret-unowned-order",
                detalles="{}",
                monto_total=50,
            ),
        ]
    )
    db.session.commit()

    response = client.get(
        "/api/public/widget-user/tenant-history",
        query_string={"tenant_slug": tenant_a.slug},
        headers={
            "X-Anon-Id": "opaque-pure-anon",
            "X-Chat-Session-Id": "unverified-session",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    titles = {item["title"] for item in payload["items"]}
    assert titles == {"exact-anonymous-claim"}
    assert payload["cart"]["items_count"] == 0
    assert "consulta_pin" not in set(_all_keys(payload))


def test_public_anon_bearer_cannot_recover_or_mutate_passkey_user(client):
    owner_a = _user("owner-a@passkey-guard.test", role="admin", tipo_chat="pyme")
    owner_b = _user("owner-b@passkey-guard.test", role="admin", tipo_chat="pyme")
    tenant_a = _tenant("passkey-guard-a", owner_a)
    tenant_b = _tenant("passkey-guard-b", owner_b)
    protected = _user(
        "protected@passkey-guard.test",
        anon_id="copied-opaque-anon",
        tenant_id=tenant_b.id,
        tenant_slug=tenant_b.slug,
        telefono="+5492615550101",
    )
    protected.name = "Protected citizen"
    db.session.add(
        WebAuthnCredential(
            user_id=protected.id,
            credential_id="passkey-guard-credential",
            public_key="passkey-guard-public-key",
            sign_count=0,
        )
    )
    attacker_context = ChatSessionContext(
        chat_session_id="attacker-session-a",
        tenant_id=tenant_a.id,
        anon_id="copied-opaque-anon",
    )
    protected_context = ChatSessionContext(
        chat_session_id="protected-session-b",
        tenant_id=tenant_b.id,
        user_id=protected.id,
        anon_id="copied-opaque-anon",
    )
    unclaimed_ticket = MunicipioTicket(
        pregunta="must remain unclaimed",
        asunto="unclaimed-attacker-ticket",
        municipio_id=owner_a.id,
        tenant_id=tenant_a.id,
        anon_id="copied-opaque-anon",
    )
    protected_ticket = TenantTicket(
        tenant_id=tenant_b.id,
        user_id=protected.id,
        categoria="protected-history-ticket",
        descripcion="must require passkey login",
    )
    attacker_cart = MarketCart(
        tenant_id=tenant_a.id,
        session_id="attacker-session-a",
        status="open",
    )
    protected_order = PymePedido(
        pyme_id=owner_b.id,
        tenant_id=tenant_b.id,
        user_id=protected.id,
        asunto="protected-history-order",
        detalles="{}",
        monto_total=100,
    )
    db.session.add_all(
        [
            attacker_context,
            protected_context,
            unclaimed_ticket,
            protected_ticket,
            attacker_cart,
            protected_order,
        ]
    )
    db.session.commit()

    headers = {
        "X-Anon-Id": "copied-opaque-anon",
        "X-Chat-Session-Id": "attacker-session-a",
    }
    register = client.post(
        "/api/public/widget-user/register",
        query_string={"tenant_slug": tenant_a.slug},
        headers=headers,
        json={
            "name": "Attacker overwrite",
            "email": "attacker-overwrite@passkey-guard.test",
            "phone": "+5492610000000",
        },
    )
    assert register.status_code == 409
    assert register.get_json()["reason_code"] == "passkey_verification_required"
    assert "profile" not in register.get_json()

    link = client.post(
        "/api/public/widget-user/link-session",
        query_string={"tenant_slug": tenant_a.slug},
        headers=headers,
    )
    assert link.status_code == 409
    assert link.get_json()["reason_code"] == "passkey_verification_required"
    assert link.get_json()["linked"] is False

    db.session.expire_all()
    protected_after = db.session.get(User, protected.id)
    assert protected_after.name == "Protected citizen"
    assert protected_after.email == "protected@passkey-guard.test"
    assert protected_after.telefono == "+5492615550101"
    assert (
        TenantFollower.query.filter_by(
            tenant_id=tenant_a.id,
            user_id=protected.id,
        ).count()
        == 0
    )
    assert db.session.get(MunicipioTicket, unclaimed_ticket.id).user_id is None
    assert db.session.get(MarketCart, attacker_cart.id).user_id is None

    protected_history = client.get(
        "/api/public/widget-user/tenant-history",
        query_string={"tenant_slug": tenant_b.slug},
        headers={
            "X-Anon-Id": "copied-opaque-anon",
            "X-Chat-Session-Id": "protected-session-b",
        },
    )
    assert protected_history.status_code == 200
    history_payload = protected_history.get_json()
    assert history_payload["cart"]["items_count"] == 0
    assert not {
        "protected-history-ticket",
        "protected-history-order",
    }.intersection({item["title"] for item in history_payload["items"]})


def test_public_register_and_link_reject_cross_tenant_session_collision(client):
    owner_a = _user("owner-a@session-collision.test", role="admin", tipo_chat="pyme")
    owner_b = _user("owner-b@session-collision.test", role="admin", tipo_chat="pyme")
    tenant_a = _tenant("session-collision-a", owner_a)
    tenant_b = _tenant("session-collision-b", owner_b)
    attacker = _user(
        "attacker@session-collision.test",
        anon_id="attacker-anon",
        tenant_id=tenant_a.id,
        tenant_slug=tenant_a.slug,
    )
    victim = _user(
        "victim@session-collision.test",
        anon_id="victim-anon",
        tenant_id=tenant_b.id,
        tenant_slug=tenant_b.slug,
    )
    victim_context = ChatSessionContext(
        chat_session_id="globally-colliding-session",
        tenant_id=tenant_b.id,
        user_id=victim.id,
    )
    unclaimed_ticket = MunicipioTicket(
        pregunta="must not merge on collision",
        municipio_id=owner_a.id,
        tenant_id=tenant_a.id,
        anon_id="attacker-anon",
    )
    db.session.add_all([victim_context, unclaimed_ticket])
    db.session.commit()

    headers = {
        "X-Anon-Id": "attacker-anon",
        "X-Chat-Session-Id": "globally-colliding-session",
    }
    register = client.post(
        "/api/public/widget-user/register",
        query_string={"tenant_slug": tenant_a.slug},
        headers=headers,
        json={
            "name": "Attacker rename",
            "phone": "+5492610000001",
        },
    )
    assert register.status_code == 409
    assert register.get_json()["reason_code"] == "session_identity_conflict"

    link = client.post(
        "/api/public/widget-user/link-session",
        query_string={"tenant_slug": tenant_a.slug},
        headers=headers,
    )
    assert link.status_code == 409
    assert link.get_json()["reason_code"] == "session_identity_conflict"
    assert link.get_json()["linked"] is False

    db.session.expire_all()
    assert db.session.get(User, attacker.id).name == "attacker"
    assert db.session.get(MunicipioTicket, unclaimed_ticket.id).user_id is None
    context_after = db.session.get(ChatSessionContext, victim_context.chat_session_id)
    assert context_after.tenant_id == tenant_b.id
    assert context_after.user_id == victim.id


def test_authenticated_viewer_cannot_mutate_different_anon_user(client):
    owner_a = _user("owner-a@authenticated-anon.test", role="admin", tipo_chat="pyme")
    owner_b = _user("owner-b@authenticated-anon.test", role="admin", tipo_chat="pyme")
    tenant_a = _tenant("authenticated-anon-a", owner_a)
    tenant_b = _tenant("authenticated-anon-b", owner_b)
    viewer_a = _user(
        "viewer-a@authenticated-anon.test",
        tenant_id=tenant_a.id,
        tenant_slug=tenant_a.slug,
        anon_id="viewer-a-anon",
    )
    victim_b = _user(
        "victim-b@authenticated-anon.test",
        tenant_id=tenant_b.id,
        tenant_slug=tenant_b.slug,
        anon_id="victim-b-anon",
        telefono="+5492615550202",
    )
    victim_b.name = "Victim B original"
    victim_context = ChatSessionContext(
        chat_session_id="victim-b-session",
        tenant_id=tenant_b.id,
        user_id=victim_b.id,
    )
    unclaimed_ticket = MunicipioTicket(
        pregunta="must not merge to victim or viewer",
        municipio_id=owner_a.id,
        tenant_id=tenant_a.id,
        anon_id="victim-b-anon",
    )
    db.session.add_all([victim_context, unclaimed_ticket])
    db.session.commit()

    login = client.post(
        "/auth/login",
        json={"email": viewer_a.email, "password": "test-pass"},
    )
    assert login.status_code == 200
    headers = {
        "Authorization": f"Bearer {login.get_json()['token']}",
        "X-Anon-Id": "victim-b-anon",
        "X-Chat-Session-Id": "victim-b-session",
    }
    register = client.post(
        "/api/public/widget-user/register",
        query_string={"tenant_slug": tenant_a.slug},
        headers=headers,
        json={
            "name": "Malicious replacement",
            "email": "replacement@authenticated-anon.test",
            "phone": "+5492610000002",
        },
    )
    assert register.status_code == 409
    assert register.get_json()["reason_code"] == "session_identity_conflict"

    link = client.post(
        "/api/public/widget-user/link-session",
        query_string={"tenant_slug": tenant_a.slug},
        headers=headers,
    )
    assert link.status_code == 409
    assert link.get_json()["reason_code"] == "session_identity_conflict"

    db.session.expire_all()
    victim_after = db.session.get(User, victim_b.id)
    assert victim_after.name == "Victim B original"
    assert victim_after.email == "victim-b@authenticated-anon.test"
    assert victim_after.telefono == "+5492615550202"
    assert db.session.get(MunicipioTicket, unclaimed_ticket.id).user_id is None
    context_after = db.session.get(ChatSessionContext, victim_context.chat_session_id)
    assert context_after.tenant_id == tenant_b.id
    assert context_after.user_id == victim_b.id


def test_authenticated_passkey_user_can_complete_own_matching_provisional_profile(client):
    owner = _user("owner@passkey-completion.test", role="admin", tipo_chat="municipio")
    tenant = _tenant("passkey-completion", owner, kind="municipio")
    provisional = _user(
        "anon-own@passkey.chatboc",
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        anon_id="own-server-anon",
    )
    provisional.name = "Ciudadano provisional"
    credential = WebAuthnCredential(
        user_id=provisional.id,
        credential_id="own-passkey-credential",
        public_key="own-passkey-public-key",
        sign_count=0,
    )
    context = ChatSessionContext(
        chat_session_id="own-passkey-session",
        tenant_id=tenant.id,
        anon_id="own-server-anon",
    )
    ticket = MunicipioTicket(
        pregunta="merge after authenticated completion",
        municipio_id=owner.id,
        tenant_id=tenant.id,
        anon_id="own-server-anon",
    )
    db.session.add_all([credential, context, ticket])
    db.session.commit()

    login = client.post(
        "/auth/login",
        json={"email": provisional.email, "password": "test-pass"},
    )
    assert login.status_code == 200
    headers = {
        "Authorization": f"Bearer {login.get_json()['token']}",
        "X-Anon-Id": "own-server-anon",
        "X-Chat-Session-Id": "own-passkey-session",
    }
    register = client.post(
        "/api/public/widget-user/register",
        query_string={"tenant_slug": tenant.slug},
        headers=headers,
        json={
            "name": "Verified citizen",
            "email": "verified@passkey-completion.test",
            "phone": "+5492615550303",
        },
    )
    assert register.status_code == 200
    register_payload = register.get_json()
    assert register_payload["status"] == "linked_existing"
    assert register_payload["profile"]["user_id"] == provisional.id

    db.session.expire_all()
    completed = db.session.get(User, provisional.id)
    assert completed.name == "Verified citizen"
    assert completed.email == "verified@passkey-completion.test"
    assert completed.telefono == "+5492615550303"
    assert db.session.get(MunicipioTicket, ticket.id).user_id == provisional.id
    completed_context = db.session.get(ChatSessionContext, context.chat_session_id)
    assert completed_context.tenant_id == tenant.id
    assert completed_context.user_id == provisional.id

    link = client.post(
        "/api/public/widget-user/link-session",
        query_string={"tenant_slug": tenant.slug},
        headers=headers,
    )
    assert link.status_code == 200
    assert link.get_json()["profile"]["user_id"] == provisional.id


def test_authenticated_viewer_can_claim_new_exact_anon_session_but_not_another_users(client):
    owner = _user("owner@new-anon-session.test", role="admin", tipo_chat="municipio")
    tenant = _tenant("new-anon-session", owner, kind="municipio")
    viewer = _user(
        "viewer@new-anon-session.test",
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        anon_id="viewer-original-anon",
    )
    other = _user(
        "other@new-anon-session.test",
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        anon_id="other-original-anon",
    )
    fresh_context = ChatSessionContext(
        chat_session_id="fresh-unclaimed-session",
        tenant_id=tenant.id,
        anon_id="fresh-unclaimed-anon",
    )
    claimed_context = ChatSessionContext(
        chat_session_id="claimed-other-session",
        tenant_id=tenant.id,
        user_id=other.id,
    )
    ticket = MunicipioTicket(
        pregunta="claim from fresh authenticated session",
        municipio_id=owner.id,
        tenant_id=tenant.id,
        anon_id="fresh-unclaimed-anon",
    )
    db.session.add_all([fresh_context, claimed_context, ticket])
    db.session.commit()

    login = client.post(
        "/auth/login",
        json={"email": viewer.email, "password": "test-pass"},
    )
    assert login.status_code == 200
    bearer = {"Authorization": f"Bearer {login.get_json()['token']}"}

    linked = client.post(
        "/api/public/widget-user/link-session",
        query_string={"tenant_slug": tenant.slug},
        headers={
            **bearer,
            "X-Anon-Id": "fresh-unclaimed-anon",
            "X-Chat-Session-Id": "fresh-unclaimed-session",
        },
    )
    assert linked.status_code == 200
    assert linked.get_json()["profile"]["user_id"] == viewer.id
    db.session.expire_all()
    assert db.session.get(ChatSessionContext, fresh_context.chat_session_id).user_id == viewer.id
    assert db.session.get(MunicipioTicket, ticket.id).user_id == viewer.id

    collision = client.post(
        "/api/public/widget-user/link-session",
        query_string={"tenant_slug": tenant.slug},
        headers={
            **bearer,
            "X-Anon-Id": "fresh-unregistered-anon",
            "X-Chat-Session-Id": "claimed-other-session",
        },
    )
    assert collision.status_code == 409
    assert collision.get_json()["reason_code"] == "session_identity_conflict"
    assert db.session.get(ChatSessionContext, claimed_context.chat_session_id).user_id == other.id


def test_merge_anon_is_tenant_scoped_and_compare_and_set_safe(client):
    owner_a = _user("owner-a@merge.test", role="admin", tipo_chat="municipio")
    owner_b = _user("owner-b@merge.test", role="admin", tipo_chat="municipio")
    tenant_a = _tenant("merge-a", owner_a, kind="municipio")
    tenant_b = _tenant("merge-b", owner_b, kind="municipio")
    target = _user("target@merge.test", tenant_id=tenant_a.id)
    other = _user("other@merge.test", tenant_id=tenant_a.id)
    anon_id = "opaque-merge"

    muni_unclaimed = MunicipioTicket(
        pregunta="unclaimed A",
        municipio_id=owner_a.id,
        tenant_id=tenant_a.id,
        anon_id=anon_id,
    )
    muni_protected = MunicipioTicket(
        pregunta="protected A",
        municipio_id=owner_a.id,
        tenant_id=tenant_a.id,
        user_id=other.id,
        anon_id=anon_id,
    )
    muni_other_tenant = MunicipioTicket(
        pregunta="unclaimed B",
        municipio_id=owner_b.id,
        tenant_id=tenant_b.id,
        anon_id=anon_id,
    )
    pyme_unclaimed = PymeTicket(
        pregunta="unclaimed pyme A",
        tenant_id=tenant_a.id,
        anon_id=anon_id,
        nro_ticket=71001,
    )
    pyme_protected = PymeTicket(
        pregunta="protected pyme A",
        tenant_id=tenant_a.id,
        user_id=other.id,
        anon_id=anon_id,
        nro_ticket=71002,
    )
    db.session.add_all(
        [muni_unclaimed, muni_protected, muni_other_tenant, pyme_unclaimed, pyme_protected]
    )
    db.session.flush()
    comment_unclaimed = TicketComentario(
        municipio_ticket_id=muni_unclaimed.id,
        comentario="merge me",
        anon_id=anon_id,
    )
    comment_protected = TicketComentario(
        municipio_ticket_id=muni_protected.id,
        comentario="keep owner",
        user_id=other.id,
        anon_id=anon_id,
    )
    suggestion_unclaimed = SugerenciaCiudadano(
        municipio_id=owner_a.id,
        texto_sugerencia="merge suggestion",
        anon_id=anon_id,
    )
    suggestion_protected = SugerenciaCiudadano(
        municipio_id=owner_a.id,
        texto_sugerencia="protected suggestion",
        user_id=other.id,
        anon_id=anon_id,
    )
    suggestion_other_tenant = SugerenciaCiudadano(
        municipio_id=owner_b.id,
        texto_sugerencia="tenant B suggestion",
        anon_id=anon_id,
    )
    survey_a = PublicSurvey(
        slug="merge-survey-a",
        titulo="A",
        estado="published",
        tenant_id=tenant_a.id,
    )
    survey_b = PublicSurvey(
        slug="merge-survey-b",
        titulo="B",
        estado="published",
        tenant_id=tenant_b.id,
    )
    db.session.add_all([survey_a, survey_b])
    db.session.flush()
    response_a = PublicSurveyResponse(survey_id=survey_a.id, anon_id=anon_id)
    response_b = PublicSurveyResponse(survey_id=survey_b.id, anon_id=anon_id)
    context_unclaimed = ChatSessionContext(
        chat_session_id="merge-session-a",
        tenant_id=tenant_a.id,
        anon_id=anon_id,
    )
    context_protected = ChatSessionContext(
        chat_session_id="merge-session-protected",
        tenant_id=tenant_a.id,
        user_id=other.id,
        anon_id=anon_id,
    )
    context_other_tenant = ChatSessionContext(
        chat_session_id="merge-session-b",
        tenant_id=tenant_b.id,
        anon_id=anon_id,
    )
    cart_unclaimed = MarketCart(
        tenant_id=tenant_a.id,
        session_id="merge-session-a",
        status="open",
    )
    cart_protected = MarketCart(
        tenant_id=tenant_a.id,
        user_id=other.id,
        session_id="merge-session-a",
        status="open",
    )
    cart_other_tenant = MarketCart(
        tenant_id=tenant_b.id,
        session_id="merge-session-a",
        status="open",
    )
    order_unclaimed = MarketOrder(
        tenant_id=tenant_a.id,
        session_id="merge-session-a",
        status="pending",
    )
    order_protected = MarketOrder(
        tenant_id=tenant_a.id,
        user_id=other.id,
        session_id="merge-session-a",
        status="pending",
    )
    order_other_tenant = MarketOrder(
        tenant_id=tenant_b.id,
        session_id="merge-session-a",
        status="pending",
    )
    db.session.add_all(
        [
            comment_unclaimed,
            comment_protected,
            suggestion_unclaimed,
            suggestion_protected,
            suggestion_other_tenant,
            response_a,
            response_b,
            context_unclaimed,
            context_protected,
            context_other_tenant,
            cart_unclaimed,
            cart_protected,
            cart_other_tenant,
            order_unclaimed,
            order_protected,
            order_other_tenant,
        ]
    )
    db.session.commit()

    stats = merge_anon_into_user(
        anon_id,
        target,
        session_ids=[
            "merge-session-a",
            "merge-session-protected",
            "merge-session-b",
        ],
        tenant_id=tenant_a.id,
    )

    assert stats == {
        "tickets": 2,
        "municipio_tickets": 1,
        "pyme_tickets": 1,
        "ticket_comentarios": 1,
        "chat_contexts": 1,
        "sugerencias": 1,
        "encuestas": 1,
        "market_carts": 1,
        "market_orders": 1,
    }
    db.session.expire_all()
    assert db.session.get(MunicipioTicket, muni_unclaimed.id).user_id == target.id
    assert db.session.get(MunicipioTicket, muni_protected.id).user_id == other.id
    assert db.session.get(MunicipioTicket, muni_other_tenant.id).user_id is None
    assert db.session.get(PymeTicket, pyme_unclaimed.id).user_id == target.id
    assert db.session.get(PymeTicket, pyme_protected.id).user_id == other.id
    assert db.session.get(TicketComentario, comment_unclaimed.id).user_id == target.id
    assert db.session.get(TicketComentario, comment_protected.id).user_id == other.id
    assert db.session.get(SugerenciaCiudadano, suggestion_unclaimed.id).user_id == target.id
    assert db.session.get(SugerenciaCiudadano, suggestion_protected.id).user_id == other.id
    assert db.session.get(SugerenciaCiudadano, suggestion_other_tenant.id).user_id is None
    assert db.session.get(PublicSurveyResponse, response_a.id).user_id == target.id
    assert db.session.get(PublicSurveyResponse, response_b.id).user_id is None
    assert db.session.get(ChatSessionContext, context_unclaimed.chat_session_id).user_id == target.id
    assert db.session.get(ChatSessionContext, context_protected.chat_session_id).user_id == other.id
    assert db.session.get(ChatSessionContext, context_other_tenant.chat_session_id).user_id is None
    assert db.session.get(MarketCart, cart_unclaimed.id).user_id == target.id
    assert db.session.get(MarketCart, cart_protected.id).user_id == other.id
    assert db.session.get(MarketCart, cart_other_tenant.id).user_id is None
    assert db.session.get(MarketOrder, order_unclaimed.id).user_id == target.id
    assert db.session.get(MarketOrder, order_protected.id).user_id == other.id
    assert db.session.get(MarketOrder, order_other_tenant.id).user_id is None


def test_crm_and_employee_history_use_historical_exact_tenant_scope(client):
    shared_owner = _user("shared-owner@scope.test", role="admin", tipo_chat="municipio")
    tenant_a = _tenant("scope-a", shared_owner, kind="municipio")
    tenant_b = TenantProfile(
        slug="scope-b",
        nombre="scope-b",
        tipo="municipio",
        plan="full",
        municipio_id=shared_owner.id,
    )
    db.session.add(tenant_b)
    db.session.flush()
    admin_a = _user("admin-a@scope.test", role="admin", tenant_id=tenant_a.id)
    employee_a = _user(
        "employee-a@scope.test",
        role="empleado",
        tenant_id=tenant_a.id,
        es_empleado=True,
    )
    employee_b = _user(
        "employee-b@scope.test",
        role="empleado",
        tenant_id=tenant_b.id,
        es_empleado=True,
    )
    legacy_ambiguous_employee = _user(
        "legacy-ambiguous@scope.test",
        role="empleado",
        empresa_id=shared_owner.id,
        es_empleado=True,
    )
    client_a = _user("client-a@scope.test", tenant_id=tenant_a.id)
    creator = _user("creator@scope.test", role="admin", tenant_id=tenant_b.id)

    note_a = ClienteNota(
        cliente_user_id=client_a.id,
        creada_por_user_id=creator.id,
        tenant_id=tenant_a.id,
        nota="historical-note-a",
    )
    note_b = ClienteNota(
        cliente_user_id=client_a.id,
        creada_por_user_id=creator.id,
        tenant_id=tenant_b.id,
        nota="secret-note-b",
    )
    note_null = ClienteNota(
        cliente_user_id=client_a.id,
        creada_por_user_id=creator.id,
        tenant_id=None,
        nota="quarantined-legacy-note",
    )
    context_a = ChatSessionContext(
        chat_session_id="llm-scope-a",
        tenant_id=tenant_a.id,
        user_id=client_a.id,
    )
    context_b = ChatSessionContext(
        chat_session_id="llm-scope-b",
        tenant_id=tenant_b.id,
        user_id=client_a.id,
    )
    db.session.add_all([note_a, note_b, note_null, context_a, context_b])
    db.session.flush()
    log_a = LlmInteractionLog(
        chat_session_id=context_a.chat_session_id,
        tenant_id=tenant_a.id,
        user_query="llm-visible-a",
    )
    log_b = LlmInteractionLog(
        chat_session_id=context_a.chat_session_id,
        tenant_id=tenant_b.id,
        user_query="llm-secret-b-even-with-a-session",
    )
    log_null = LlmInteractionLog(
        chat_session_id=context_b.chat_session_id,
        tenant_id=None,
        user_query="llm-quarantined-null",
    )
    ticket_a = MunicipioTicket(
        pregunta="ticket A",
        municipio_id=shared_owner.id,
        tenant_id=tenant_a.id,
    )
    ticket_b = MunicipioTicket(
        pregunta="ticket B",
        municipio_id=shared_owner.id,
        tenant_id=tenant_b.id,
    )
    db.session.add_all([log_a, log_b, log_null, ticket_a, ticket_b])
    db.session.flush()
    comment_a = TicketComentario(
        municipio_ticket_id=ticket_a.id,
        comentario="employee-visible-a",
        user_id=employee_a.id,
        es_admin=True,
    )
    comment_b = TicketComentario(
        municipio_ticket_id=ticket_b.id,
        comentario="employee-secret-b",
        user_id=employee_a.id,
        es_admin=True,
    )
    db.session.add_all([comment_a, comment_b])
    db.session.commit()

    # Moving the creator must not move a historical note into or out of scope.
    creator.tenant_id = tenant_a.id
    db.session.commit()

    assert {row.id for row in _crm_note_query(admin_a, cliente_id=client_a.id).all()} == {
        note_a.id
    }
    assert {row.id for row in _crm_llm_log_query(admin_a).all()} == {log_a.id}
    assert {row.id for row in _empleados_query(admin_a).all()} == {employee_a.id}
    assert legacy_ambiguous_employee.id not in {
        row.id for row in _empleados_query(admin_a).all()
    }
    assert employee_b.id not in {row.id for row in _empleados_query(admin_a).all()}
    assert {
        row.id for row in _employee_comment_query(admin_a, employee_a.id).all()
    } == {comment_a.id}
