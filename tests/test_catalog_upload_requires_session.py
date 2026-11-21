import io

import pytest

from database import db
from models import TenantProfile, User
from utils.auth_helpers import generar_token


def _owner():
    owner = User(name="Dueño", email="owner-upload@example.test", rol="admin", tipo_chat="pyme",
                 password_hash="synthetic")
    db.session.add(owner)
    db.session.flush()
    owner.token = "synthetic-owner-value"
    tenant = TenantProfile(slug="upload-fixture", nombre="Comercio de prueba", tipo="pyme",
                           pyme_id=owner.id, plan="full")
    db.session.add(tenant)
    db.session.commit()
    return owner


def _csv():
    return {"file": (io.BytesIO(b"nombre,precio\nYerba,100\n"), "catalogo.csv")}


@pytest.mark.parametrize("headers", [
    lambda owner: {"Authorization": f"Bearer {owner.token}"},
    lambda owner: {"X-Entity-Token": owner.token},
    lambda owner: {"X-Widget-Token": owner.token},
    lambda owner: {},
])
def test_direct_upload_rejects_anything_but_a_session(client, headers, monkeypatch):
    owner = _owner()
    reached = []
    monkeypatch.setattr("services.upload_processor.validate_upload_size", lambda *a, **k: reached.append(True))
    response = client.post("/subir_catalogo", data=_csv(), headers=headers(owner),
                           content_type="multipart/form-data")
    assert response.status_code in (401, 403)
    assert not reached


def test_direct_upload_accepts_the_owner_session(client, monkeypatch):
    owner = _owner()
    reached = []

    def stop(*_a, **_k):
        reached.append(True)
        raise RuntimeError("stop after auth")

    monkeypatch.setattr("services.upload_processor.validate_upload_size", stop)
    token = generar_token(owner.id, owner.rol, owner.tipo_chat, owner.municipio_id, owner.pyme_id)
    try:
        client.post("/subir_catalogo", data=_csv(), headers={"Authorization": f"Bearer {token}"},
                    content_type="multipart/form-data")
    except RuntimeError:
        pass
    assert reached
