import pytest
from unittest.mock import patch
from services.flows.noticias import handle as handle_noticias

@patch('services.flows.noticias.google_search')
def test_noticias_flow_happy_path(mock_google_search):
    """
    Tests the happy path for the noticias flow.
    """
    mock_google_search.return_value = [
        {"title": "Nueva ciclovía en Junín", "link": "https://junin.gob.ar/ciclovia", "snippet": "..."},
        {"title": "Festival de la Vendimia 2025", "link": "https://junin.gob.ar/vendimia", "snippet": "..."}
    ]

    msg = {"text": "noticias"}
    ctx = {"user_obj": {"nombre_empresa": "Junín"}}

    response = handle_noticias(msg, ctx)

    assert "últimas noticias sobre Junín" in response['message_body']
    assert len(response['options_list']) == 2
    assert response['options_list'][0]['texto'] == "Nueva ciclovía en Junín"
    assert response['options_list'][0]['url'] == "https://junin.gob.ar/ciclovia"
    mock_google_search.assert_called_once_with("noticias de Junín", days=7)

@patch('services.flows.noticias.google_search', return_value=[])
def test_noticias_flow_no_results(mock_google_search):
    """
    Tests the flow when google_search returns no results.
    """
    msg = {"text": "noticias"}
    ctx = {"user_obj": {"nombre_empresa": "Junín"}}
    response = handle_noticias(msg, ctx)
    assert "No encontré noticias recientes" in response['message_body']
