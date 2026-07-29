import unittest
from unittest.mock import patch, MagicMock
import sys
import os
from types import SimpleNamespace
from datetime import datetime, timezone

# Añadir el directorio raíz al path para importar módulos de la app
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from app import create_app, db
from models import PlantillasRespuesta, TenantProfile, User, Rubro
from routes.ai import ai_bp as ai_templates_bp
import json
import pytest
from config import Config
import jwt
from datetime import datetime, timedelta

# Configuración de prueba
from config import Config

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    COHERE_API_KEY = "test_cohere_key"

def _crear_plantilla(name, text, keywords=None, is_active=True, embedding_value=None, tenant_id=None):
    if embedding_value is None:
        embedding_value = [0.1] * 1024

    plantilla = PlantillasRespuesta(
        name=name, text=text,
        tenant_id=tenant_id,
        keywords=json.dumps(keywords) if keywords else json.dumps([]),
        is_active=is_active, embedding=embedding_value
    )
    db.session.add(plantilla)
    db.session.commit()
    return plantilla


def _jwt_headers(app, user, tenant_slug=None):
    jwt_payload = {
        'user_id': user.id,
        'exp': datetime.utcnow() + timedelta(days=1)
    }
    jwt_token = jwt.encode(jwt_payload, app.config['SECRET_KEY'], algorithm="HS256")
    headers = {'Authorization': f'Bearer {jwt_token}'}
    if tenant_slug:
        headers['X-Tenant-Slug'] = tenant_slug
    return headers

def test_suggest_templates_success(client):
    rubro = Rubro(id=1, clave="pyme_test_rubro", nombre="Test Rubro PYME")
    db.session.add(rubro)
    db.session.commit()

    mock_user = User(
        id=1, name="Test Admin User", email="admin@test.com",
        rol="admin", rubro_id=rubro.id
    )
    mock_user.set_password("adminpass")
    db.session.add(mock_user)
    db.session.commit()

    # Generate JWT for the mock user
    jwt_payload = {
        'user_id': mock_user.id,
        'exp': datetime.utcnow() + timedelta(days=1)
    }
    # We need access to the app to get the secret key
    jwt_token = jwt.encode(jwt_payload, client.application.config['SECRET_KEY'], algorithm="HS256")

    saludo_embedding = [1.0] + [0.0] * 1023
    despedida_embedding = [0.0, 1.0] + [0.0] * 1022
    _crear_plantilla(
        "Saludo",
        "Hola, ¿cómo estás {{nombre_cliente}}?",
        ["saludo"],
        embedding_value=saludo_embedding,
    )
    _crear_plantilla(
        "Despedida",
        "Adiós, {{nombre_cliente}}.",
        ["despedida"],
        embedding_value=despedida_embedding,
    )

    with patch('routes.ai.embed_textos_llm') as mock_embed_textos_llm:
        mock_embed_textos_llm.return_value = [saludo_embedding]
        response = client.post('/api/ai/suggest-templates',
                                    headers={'Authorization': f'Bearer {jwt_token}'},
                                    json={'asunto': 'Quiero saludar', 'contexto_ticket': 'Hola', 'top_n': 1})

        assert response.status_code == 200
        data = response.get_json()
        assert 'sugerencias' in data
        sugerencias = data['sugerencias']
        assert len(sugerencias) >= 1
        assert sugerencias[0]['name'] == 'Saludo'
        assert "Hola, ¿cómo estás {{nombre_cliente}}?" in sugerencias[0]['text']
        mock_embed_textos_llm.assert_called_once()


def test_suggest_templates_logs_no_request_or_provider_pii(client):
    rubro = Rubro(clave="pyme_private_logs", nombre="Private Logs")
    db.session.add(rubro)
    db.session.flush()

    user = User(
        id=987654,
        name="Private Log Admin",
        email="private-log-admin@example.test",
        rol="admin",
        rubro_id=rubro.id,
    )
    user.set_password("adminpass")
    db.session.add(user)
    db.session.commit()

    private_subject = "reclamo privado DNI 32877851"
    private_provider_detail = "provider-secret-32877851"
    headers = _jwt_headers(client.application, user)

    with patch.object(client.application.logger, "info") as info_log, patch.object(
        client.application.logger,
        "error",
    ) as error_log, patch(
        "routes.ai.embed_textos_llm",
        side_effect=RuntimeError(private_provider_detail),
    ):
        response = client.post(
            "/api/ai/suggest-templates",
            headers=headers,
            json={"asunto": private_subject, "contexto_ticket": "contexto privado"},
        )

    assert response.status_code == 500
    log_calls = info_log.call_args_list + error_log.call_args_list
    rendered_logs = "\n".join(
        call.args[0] % call.args[1:] if len(call.args) > 1 else call.args[0]
        for call in log_calls
    )
    assert private_subject not in rendered_logs
    assert private_provider_detail not in rendered_logs
    assert str(user.id) not in rendered_logs
    assert "error_type=RuntimeError" in rendered_logs
    assert all(not call.kwargs.get("exc_info") for call in error_log.call_args_list)


def test_ai_templates_crud_is_scoped_by_tenant(client):
    owner_a = User(email="templates-a@test.com", name="Templates A", rol="admin", tipo_chat="pyme")
    owner_a.set_password("pass")
    owner_b = User(email="templates-b@test.com", name="Templates B", rol="admin", tipo_chat="pyme")
    owner_b.set_password("pass")
    db.session.add_all([owner_a, owner_b])
    db.session.flush()

    tenant_a = TenantProfile(slug="templates-a", nombre="Templates A", tipo="pyme", pyme_id=owner_a.id)
    tenant_b = TenantProfile(slug="templates-b", nombre="Templates B", tipo="pyme", pyme_id=owner_b.id)
    db.session.add_all([tenant_a, tenant_b])
    db.session.commit()

    global_tpl = _crear_plantilla("Global base", "Texto global", ["global"])
    a_tpl = _crear_plantilla("Solo A", "Texto A", ["a"], tenant_id=tenant_a.id)
    b_tpl = _crear_plantilla("Solo B", "Texto B", ["b"], tenant_id=tenant_b.id)

    response = client.get(
        "/api/ai/templates",
        headers=_jwt_headers(client.application, owner_a, tenant_a.slug),
    )
    assert response.status_code == 200
    names = {item["name"] for item in response.get_json()["plantillas"]}
    assert names == {global_tpl.name, a_tpl.name}
    assert b_tpl.name not in names

    with patch("routes.ai_templates.embed_textos_llm", return_value=[[0.3] * 4]):
        create_resp = client.post(
            "/api/ai/templates",
            headers=_jwt_headers(client.application, owner_a, tenant_a.slug),
            json={"name": "Nueva A", "text": "Respuesta tenant A", "keywords": ["a"]},
        )
    assert create_resp.status_code == 201
    assert create_resp.get_json()["tenant_id"] == tenant_a.id

    delete_cross = client.delete(
        f"/api/ai/templates/{b_tpl.id}",
        headers=_jwt_headers(client.application, owner_a, tenant_a.slug),
    )
    assert delete_cross.status_code == 404

    delete_own = client.delete(
        f"/api/ai/templates/{a_tpl.id}",
        headers=_jwt_headers(client.application, owner_a, tenant_a.slug),
    )
    assert delete_own.status_code == 200

if __name__ == '__main__':
    unittest.main()
