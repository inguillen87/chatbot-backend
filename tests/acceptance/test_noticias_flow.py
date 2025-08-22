import pytest
from unittest.mock import patch, MagicMock
from services.flows.noticias import handle as handle_noticias
from models import MunicipioTicket, db
from app import create_app
from config import TestConfig
from datetime import datetime

@pytest.fixture
def app_context():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        yield
        db.session.remove()
        db.drop_all()

def test_noticias_flow_happy_path(app_context):
    """
    Tests the happy path for the noticias flow fetching from the database.
    """
    # Create mock data in the test database
    mock_news_item_1 = MunicipioTicket(asunto="Nueva ciclovía en Junín", detalles="Se inauguró una nueva ciclovía...", categoria="Noticia", fecha=datetime.now(), foto_url_directa="http://example.com/foto1.jpg", municipio_id=1, nro_ticket="T1")
    mock_news_item_2 = MunicipioTicket(asunto="Festival de la Vendimia 2025", detalles="El festival se realizará en...", categoria="Evento", fecha=datetime.now(), foto_url_directa="http://example.com/foto2.jpg", municipio_id=1, nro_ticket="T2")
    db.session.add_all([mock_news_item_1, mock_news_item_2])
    db.session.commit()

    msg = "noticias"
    meta = {"municipio_id": 1}

    response = handle_noticias(msg, meta)

    assert response['type'] == 'noticias'
    assert "Últimas noticias y eventos" in response['title']
    assert len(response['data']['items']) == 2
    assert response['data']['items'][0]['title'] == "Festival de la Vendimia 2025" # Desc order
    assert response['data']['items'][1]['summary'] == "Se inauguró una nueva ciclovía..."
    assert response['source'] == 'database'


def test_noticias_flow_no_results(app_context):
    """
    Tests the flow when the database returns no results.
    """
    msg = "noticias"
    meta = {"municipio_id": 1}

    response = handle_noticias(msg, meta)

    assert response['type'] == 'noticias'
    assert "No se encontraron noticias" in response['title']
    assert len(response['data']['items']) == 0
