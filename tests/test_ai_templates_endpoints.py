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
from models import PlantillasRespuesta, User, Rubro
from routes.ai_templates import ai_templates_bp
import json
import pytest
import pytest
from config import Config

# Configuración de prueba
from config import Config

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    COHERE_API_KEY = "test_cohere_key"

def _crear_plantilla(name, text, keywords=None, is_active=True, embedding_value=None):
    if embedding_value is None:
        embedding_value = [0.1] * 1024

    plantilla = PlantillasRespuesta(
        name=name, text=text,
        keywords=json.dumps(keywords) if keywords else json.dumps([]),
        is_active=is_active, embedding=embedding_value
    )
    db.session.add(plantilla)
    db.session.commit()
    return plantilla

@pytest.fixture
def test_app():
    app = create_app(TestConfig)
    app.register_blueprint(ai_templates_bp, url_prefix='/ai')
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()

def test_suggest_templates_success(test_app):
    with test_app.app_context():
        rubro = Rubro(id=1, clave="pyme_test_rubro", nombre="Test Rubro PYME")
        db.session.add(rubro)
        db.session.commit()

        mock_user = User(
            id=1, name="Test Admin User", email="admin@test.com",
            rol="admin", token="test_auth_token_admin", rubro_id=rubro.id
        )
        mock_user.set_password("adminpass")
        db.session.add(mock_user)
        db.session.commit()
        _crear_plantilla("Saludo", "Hola, ¿cómo estás {{nombre_cliente}}?", ["saludo"], embedding_value=[0.1]*1024)
        _crear_plantilla("Despedida", "Adiós, {{nombre_cliente}}.", ["despedida"], embedding_value=[0.2]*1024)

        with patch('services.cohere_ai.robust_embed') as mock_robust_embed:
            mock_robust_embed.return_value = {"embeddings": [[0.11]*1024]}
            client = test_app.test_client()
            response = client.post('/ai/suggest-templates',
                                        headers={'Authorization': f'Bearer {mock_user.token}'},
                                        json={'asunto': 'Quiero saludar', 'consulta_cliente': 'Hola', 'top_n': 1})

            assert response.status_code == 200
            data = response.get_json()
            assert 'sugerencias' in data
            sugerencias = data['sugerencias']
            assert len(sugerencias) == 1
            assert sugerencias[0]['nombre_plantilla'] == 'Saludo'
            assert "Hola, ¿cómo estás {{nombre_cliente}}?" in sugerencias[0]['texto_plantilla']
            mock_robust_embed.assert_called_once()

if __name__ == '__main__':
    unittest.main()
