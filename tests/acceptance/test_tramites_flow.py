import pytest
from unittest.mock import patch
from services.flows.tramites import handle as handle_tramites, CACHE

@pytest.fixture(autouse=True)
def clear_cache():
    """Ensures the cache is clear before each test."""
    CACHE.clear()

@patch('services.flows.tramites.google_search')
def test_tramites_flow_happy_path_and_caching(mock_google_search):
    """
    Tests the happy path for the tramites flow and verifies that caching works.
    """
    mock_google_search.return_value = [{
        "title": "Licencia de Conducir - Municipalidad de Junín",
        "link": "https://junin.gob.ar/licencia",
        "snippet": "Requisitos para obtener la licencia de conducir por primera vez..."
    }]

    msg = {"text": "licencia de conducir"}
    ctx = {"user_obj": {"nombre_empresa": "Junín"}}

    # 1. First call, should call google_search
    response1 = handle_tramites(msg, ctx)

    assert "Licencia de Conducir - Municipalidad de Junín" in response1['message_body']
    assert "https://junin.gob.ar/licencia" in response1['message_body']
    mock_google_search.assert_called_once_with("tramite licencia de conducir en Junín")

    # 2. Second call with same query, should use cache
    response2 = handle_tramites(msg, ctx)

    assert "Licencia de Conducir - Municipalidad de Junín" in response2['message_body']
    mock_google_search.assert_called_once() # Should NOT be called again

def test_tramites_flow_no_results():
    """
    Tests the flow when google_search returns no results.
    """
    with patch('services.flows.tramites.google_search', return_value=[]):
        msg = {"text": "un tramite inexistente"}
        ctx = {"user_obj": {"nombre_empresa": "Junín"}}
        response = handle_tramites(msg, ctx)
        assert "No encontré información sobre el trámite" in response['message_body']
