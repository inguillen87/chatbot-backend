from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest

from app import db
from models import TenantProfile, User
from models_memory import Contact, ContactSnapshot, InteractionEvent
from routes.crm.routes import _serialize_cliente
from services.crm_intelligence import serialize_crm_contact
from services.crm_output_safety import (
    CRM_SENSITIVE_CONTENT_PLACEHOLDER,
    contains_crm_sensitive_content,
    redact_crm_sensitive_value,
)


@pytest.mark.parametrize(
    "value",
    [
        "17049 es tu código de Instagram. No lo compartas.",
        "Tu código es 614203",
        "Código de WhatsApp: 773901",
        "OTP: 482911",
        "PIN de acceso 9384",
        "passcode=ABCD1234",
        "contraseña: temporal-2026",
        "api_key=sk-demo1234567890",
        "clave de API: abcdefghijklmnop",
        "access_token: eyJhbGciOiJIUzI1NiJ9.demo.signature",
        "Authorization: Bearer abcdefghijklmnop",
        "sk-proj-abcdefghijklmnop",
    ],
)
def test_sensitive_content_detector_redacts_high_confidence_credentials(value):
    assert contains_crm_sensitive_content(value) is True
    assert redact_crm_sensitive_value(value) == CRM_SENSITIVE_CONTENT_PLACEHOLDER


@pytest.mark.parametrize(
    "value",
    [
        "Reclamo M-195268 por luminaria apagada en calle 1234.",
        "Código postal 5500, barrio Centro, Junín.",
        "Bache en Ruta 7, kilómetro 258.",
        "La orden 17049 corresponde a recambio de luminarias.",
        "Necesito revisar el token de participación ciudadana del proyecto, sin credenciales.",
    ],
)
def test_sensitive_content_detector_preserves_ordinary_municipal_context(value):
    assert contains_crm_sensitive_content(value) is False
    assert redact_crm_sensitive_value(value) == value


def test_nested_redaction_returns_copy_without_mutating_persisted_source():
    source = {
        "last_message_excerpt": "OTP: 482911",
        "suggested_actions": [
            "Revisar ticket M-419",
            {"label": "access_token=abcdefghijklmnop"},
        ],
    }

    sanitized = redact_crm_sensitive_value(source)

    assert sanitized == {
        "last_message_excerpt": CRM_SENSITIVE_CONTENT_PLACEHOLDER,
        "suggested_actions": [
            "Revisar ticket M-419",
            {"label": CRM_SENSITIVE_CONTENT_PLACEHOLDER},
        ],
    }
    assert source["last_message_excerpt"] == "OTP: 482911"
    assert source["suggested_actions"][1]["label"] == "access_token=abcdefghijklmnop"


def _contact(*, name: str, preferences: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id="contact-1",
        name=name,
        phone="+5492613000000",
        email="vecino@example.com",
        preferences=preferences,
        tags=["municipio", "luminarias"],
        created_at=datetime(2026, 8, 27, tzinfo=timezone.utc),
        last_interaction_at=datetime(2026, 8, 27, tzinfo=timezone.utc),
        type="neighbor",
        ltv_monetary=0,
        total_orders=0,
    )


def test_crm_contact_serializer_redacts_every_sensitive_display_projection():
    raw_secret = "17049 es tu código de Instagram. No lo compartas."
    contact = _contact(
        name=raw_secret,
        preferences={
            "last_channel": "whatsapp",
            "last_source": "junin",
            "last_summary": f"Consulta general: {raw_secret}",
            "last_reason": "PIN: 9384",
            "last_intent": "access_token=abcdefghijklmnop",
            "suggested_actions": ["Revisar ticket M-419", "OTP: 482911"],
            "last_message_excerpt": "password: temporal-2026",
        },
    )
    snapshot = SimpleNamespace(
        summary_text=f"Historial: {raw_secret}",
        last_intent="OTP: 482911",
        suggested_actions=["Revisar ticket M-419", "api_key=abcdefghijklmnop"],
    )

    payload = serialize_crm_contact(contact, snapshot=snapshot, interaction_count=3)

    assert payload["name"] == "Contacto WhatsApp"
    assert payload["name_quality"] == "sensitive_redacted"
    for key in (
        "raw_name",
        "profile_excerpt",
        "summary",
        "conversation_summary",
        "motivo",
        "last_intent",
        "last_message_excerpt",
    ):
        assert payload[key] == CRM_SENSITIVE_CONTENT_PLACEHOLDER
    assert payload["suggested_actions"] == [
        "Revisar ticket M-419",
        CRM_SENSITIVE_CONTENT_PLACEHOLDER,
    ]
    assert contact.name == raw_secret
    assert contact.preferences["last_message_excerpt"] == "password: temporal-2026"


def test_crm_contact_serializer_preserves_municipal_operational_context():
    summary = "Reclamo M-419 por luminaria en calle 1234, código postal 5500."
    contact = _contact(
        name="María Vecina",
        preferences={
            "last_channel": "whatsapp",
            "last_source": "junin",
            "last_summary": summary,
            "last_reason": "Reclamo de alumbrado público",
            "last_intent": "reclamo_luminaria",
            "suggested_actions": ["Asignar cuadrilla 7", "Revisar ticket M-419"],
            "last_message_excerpt": "Luminaria 17049 apagada en Ruta 7.",
        },
    )
    snapshot = SimpleNamespace(
        summary_text=summary,
        last_intent="reclamo_luminaria",
        suggested_actions=["Asignar cuadrilla 7"],
    )

    payload = serialize_crm_contact(contact, snapshot=snapshot)

    assert payload["name"] == "María Vecina"
    assert payload["raw_name"] == "María Vecina"
    assert payload["summary"] == summary
    assert payload["motivo"] == "Reclamo de alumbrado público"
    assert payload["last_message_excerpt"] == "Luminaria 17049 apagada en Ruta 7."
    assert payload["suggested_actions"] == ["Asignar cuadrilla 7"]


def test_legacy_crm_serializer_redacts_message_shaped_name_without_contact():
    raw_secret = "482911 es tu código de seguridad. No lo compartas."
    cliente = SimpleNamespace(
        id=91,
        name=raw_secret,
        email="whatsapp-91@placeholder.local",
        telefono="+5492613000091",
        acepta_marketing=False,
        tags="municipio,luminarias",
        latitud=-33.0803,
        longitud=-68.4681,
        fecha_creacion=datetime(2026, 8, 27, tzinfo=timezone.utc),
    )

    payload = _serialize_cliente(cliente)

    assert payload["name"] == "Contacto WhatsApp"
    assert payload["name_quality"] == "sensitive_redacted"
    assert payload["raw_name"] == CRM_SENSITIVE_CONTENT_PLACEHOLDER
    assert payload["profile_excerpt"] == CRM_SENSITIVE_CONTENT_PLACEHOLDER
    assert cliente.name == raw_secret


def test_crm_history_endpoint_redacts_display_copy_and_keeps_database_source(client, app):
    raw_secret = "Tu código es 614203. No lo compartas."
    admin = User(
        email="crm-output-safety-admin@test.com",
        name="Admin CRM",
        rol="admin",
        tipo_chat="municipio",
    )
    admin.set_password("pass")
    db.session.add(admin)
    db.session.flush()
    tenant = TenantProfile(
        slug="crm-output-safety-junin",
        nombre="Municipalidad de Junín",
        tipo="municipio",
        municipio_id=admin.id,
    )
    db.session.add(tenant)
    db.session.flush()
    admin.tenant_id = tenant.id
    admin.tenant_slug = tenant.slug
    contact = Contact(
        id=str(uuid4()),
        tenant_id=tenant.id,
        name=raw_secret,
        phone="+5492613000042",
        type="neighbor",
        tags=["luminarias"],
        preferences={
            "last_summary": raw_secret,
            "last_reason": "Reclamo M-419 por luminaria en calle 1234.",
            "last_message_excerpt": "OTP: 482911",
        },
    )
    db.session.add(contact)
    db.session.flush()
    db.session.add(
        ContactSnapshot(
            contact_id=contact.id,
            summary_text=raw_secret,
            last_intent="access_token=abcdefghijklmnop",
            suggested_actions=["Revisar ticket M-419", "PIN: 9384"],
        )
    )
    db.session.add(
        InteractionEvent(
            tenant_id=tenant.id,
            contact_id=contact.id,
            channel="whatsapp",
            direction="inbound",
            content_type="text",
            content=raw_secret,
            metadata_payload={"source": "junin"},
        )
    )
    db.session.commit()

    token = jwt.encode(
        {
            "user_id": admin.id,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    response = client.get(
        f"/api/admin/tenants/{tenant.slug}/contacts/{contact.id}/history",
        headers={"Authorization": f"Bearer {token}", "X-Tenant": tenant.slug},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["contact"]["name"] == CRM_SENSITIVE_CONTENT_PLACEHOLDER
    assert payload["contact"]["preferences"]["last_summary"] == CRM_SENSITIVE_CONTENT_PLACEHOLDER
    assert payload["contact"]["preferences"]["last_reason"] == (
        "Reclamo M-419 por luminaria en calle 1234."
    )
    assert payload["snapshot"] == {
        "summary": CRM_SENSITIVE_CONTENT_PLACEHOLDER,
        "last_intent": CRM_SENSITIVE_CONTENT_PLACEHOLDER,
        "suggested_actions": [
            "Revisar ticket M-419",
            CRM_SENSITIVE_CONTENT_PLACEHOLDER,
        ],
    }
    assert payload["interactions"][0]["content"] == CRM_SENSITIVE_CONTENT_PLACEHOLDER

    persisted = Contact.query.filter_by(id=contact.id).one()
    persisted_snapshot = ContactSnapshot.query.filter_by(contact_id=contact.id).one()
    persisted_event = InteractionEvent.query.filter_by(contact_id=contact.id).one()
    assert persisted.name == raw_secret
    assert persisted.preferences["last_summary"] == raw_secret
    assert persisted_snapshot.summary_text == raw_secret
    assert persisted_event.content == raw_secret
