import pytest

from models import db, User, Rubro, WhatsappNumero
from services.user_service import assign_whatsapp_numbers


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
