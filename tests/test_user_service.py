import pytest

from models import db, User, Rubro, WhatsappNumero
from services.user_service import (
    assign_whatsapp_numbers,
    build_identity_subject,
    get_user_profile_identity,
    set_user_profile_avatar,
)


@pytest.fixture
def bodega_rubro(client):
    rubro = Rubro(clave="bodega", nombre="Bodega")
    db.session.add(rubro)
    db.session.commit()
    return rubro


def create_user(email: str, rubro_id: int, tipo_chat: str = "pyme", nombre_empresa: str = "Bodega Cuatro Fincas") -> User:
    user = User(
        email=email,
        name=email.split("@")[0],
        nombre_empresa=nombre_empresa,
        rubro_id=rubro_id,
        tipo_chat=tipo_chat,
    )
    user.set_password("123456")
    db.session.add(user)
    db.session.commit()
    return user


CUATRO_FINCAS_WHATSAPP = "+18564858589"


def test_profile_avatar_accepts_consented_oauth_source_prefix(client, bodega_rubro):
    user = create_user("social-avatar@test.com", bodega_rubro.id)

    success, message, status = set_user_profile_avatar(
        user,
        "https://cdn.example.com/profile/social-avatar.webp",
        source="oauth_google",
        commit=True,
    )

    assert success is True
    assert status == 200
    assert "Avatar" in message

    identity = get_user_profile_identity(user)
    assert identity["avatar_url"] == "https://cdn.example.com/profile/social-avatar.webp"
    assert identity["avatar_source"] == "oauth_google"
    assert identity["avatar_consent"] is True

    subject = build_identity_subject(user=user)
    assert subject["avatar_url"] == "https://cdn.example.com/profile/social-avatar.webp"
    assert subject["avatar_source"] == "oauth_google"
    assert subject["avatar_policy"] == "consented_upload_or_social_only"
    assert subject["fallback"] == "deterministic_identity_avatar"


@pytest.mark.parametrize("source", ["agent_profile", "staff_profile", "employee_profile"])
def test_profile_avatar_accepts_consented_internal_staff_sources(client, bodega_rubro, source):
    user = create_user(f"{source}@test.com", bodega_rubro.id)

    success, message, status = set_user_profile_avatar(
        user,
        "https://cdn.example.com/profile/operator.webp",
        source=source,
        commit=True,
    )

    assert success is True
    assert status == 200
    assert "Avatar" in message
    assert get_user_profile_identity(user)["avatar_source"] == source


def test_profile_avatar_rejects_generic_contact_profile_source(client, bodega_rubro):
    user = create_user("generic-contact-profile@test.com", bodega_rubro.id)

    success, message, status = set_user_profile_avatar(
        user,
        "https://cdn.example.com/profile/contact.webp",
        source="contact_profile",
        commit=True,
    )

    assert success is False
    assert status == 400
    assert "fuente" in message.lower()
    assert get_user_profile_identity(user)["avatar_url"] is None


@pytest.mark.parametrize(
    "source",
    [
        "whatsapp-profile",
        "WhatsApp Avatar",
        "wa photo",
        "profile scrape",
        "mock/avatar",
        "fake portrait",
        "synthetic-generated",
        "whatsapp_media_upload",
    ],
)
def test_profile_avatar_rejects_untrusted_source_variants(client, bodega_rubro, source):
    user = create_user(f"unsafe-{source.replace(' ', '-').replace('/', '-')}@test.com", bodega_rubro.id)

    success, message, status = set_user_profile_avatar(
        user,
        "https://cdn.example.com/profile/unsafe.webp",
        source=source,
        commit=True,
    )

    assert success is False
    assert status == 400
    assert "fuente" in message.lower()
    assert get_user_profile_identity(user)["avatar_url"] is None


def test_assign_whatsapp_creates_new_mapping(client, bodega_rubro):
    franco = create_user("franco@cuatrofincas.com", bodega_rubro.id)

    results = assign_whatsapp_numbers(franco, [CUATRO_FINCAS_WHATSAPP], commit=True)

    assert len(results) == 1
    result = results[0]
    assert result["status"] == "created"
    mapping = WhatsappNumero.query.filter_by(numero_whatsapp=CUATRO_FINCAS_WHATSAPP).first()
    assert mapping is not None
    assert mapping.user_id == franco.id
    assert mapping.is_active is True


def test_assign_whatsapp_reassigns_and_reactivates(client, bodega_rubro):
    original_owner = create_user("demo@chatboc.ar", bodega_rubro.id)
    franco = create_user("franco@cuatrofincas.com", bodega_rubro.id)

    mapping = WhatsappNumero(numero_whatsapp=CUATRO_FINCAS_WHATSAPP, user_id=original_owner.id, is_active=False)
    db.session.add(mapping)
    db.session.commit()

    results = assign_whatsapp_numbers(franco, [CUATRO_FINCAS_WHATSAPP], commit=True)

    assert len(results) == 1
    result = results[0]
    assert result["status"] == "reassigned"
    assert result["previous_user_id"] == original_owner.id
    assert result["previous_user_email"] == original_owner.email
    assert result["reactivated"] is True

    mapping = WhatsappNumero.query.filter_by(numero_whatsapp=CUATRO_FINCAS_WHATSAPP).first()
    assert mapping.user_id == franco.id
    assert mapping.is_active is True


def test_assign_whatsapp_reactivates_existing_mapping(client, bodega_rubro):
    franco = create_user("franco@cuatrofincas.com", bodega_rubro.id)
    mapping = WhatsappNumero(numero_whatsapp=CUATRO_FINCAS_WHATSAPP, user_id=franco.id, is_active=False)
    db.session.add(mapping)
    db.session.commit()

    results = assign_whatsapp_numbers(franco, [CUATRO_FINCAS_WHATSAPP, " ", None], commit=True)

    assert len(results) == 1
    result = results[0]
    assert result["status"] == "reactivated"
    assert result["reactivated"] is True

    mapping = WhatsappNumero.query.filter_by(numero_whatsapp=CUATRO_FINCAS_WHATSAPP).first()
    assert mapping.user_id == franco.id
    assert mapping.is_active is True
