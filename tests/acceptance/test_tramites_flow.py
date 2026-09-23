import pytest
from unittest.mock import patch
from services.flows.tramites import handle as handle_tramites, CACHE

@pytest.fixture(autouse=True)
def clear_cache():
    """Ensures the cache is clear before each test."""
    CACHE.clear()

@pytest.mark.legacy
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

    msg = "licencia de conducir"
    ctx = {"tenant_slug": "junin", "user_obj": {"nombre_empresa": "Junín"}}

    # 1. First call, should call google_search
    response1 = handle_tramites(msg, ctx)

    assert "Licencia de Conducir - Municipalidad de Junín" in response1['message_body']
    assert "https://junin.gob.ar/licencia" in response1['message_body']
    mock_google_search.assert_called_once_with("tramite licencia de conducir en Junín")

    # 2. Second call with same query, should use cache
    response2 = handle_tramites(msg, ctx)

    assert "Licencia de Conducir - Municipalidad de Junín" in response2['message_body']
    mock_google_search.assert_called_once() # Should NOT be called again

@pytest.mark.legacy
def test_tramites_flow_no_results():
    """
    Tests the flow when google_search returns no results.
    """
    with patch('services.flows.tramites.google_search', return_value=[]):
        msg = "un tramite inexistente"
        ctx = {"user_obj": {"nombre_empresa": "Junín"}}
        response = handle_tramites(msg, ctx)
        assert "No encontré información sobre el trámite" in response['message_body']


@pytest.mark.legacy
@patch('services.flows.tramites.google_search')
def test_tramites_cache_is_isolated_by_tenant_and_uses_configured_identity(mock_google_search):
    mock_google_search.side_effect = [
        [{
            "title": "Licencia - Municipalidad de Junín",
            "link": "https://junin.example/licencia",
            "snippet": "Información de Junín",
        }],
        [{
            "title": "Licencia - Municipalidad de Ushuaia",
            "link": "https://ushuaia.example/licencia",
            "snippet": "Información de Ushuaia",
        }],
    ]
    msg = "licencia de conducir"
    junin_ctx = {
        "tenant_slug": "junin",
        "municipio_config_actual": {"nombre_municipio": "Municipalidad de Junín"},
    }
    ushuaia_ctx = {
        "tenant_slug": "ushuaia",
        "municipio_config_actual": {"nombre_municipio": "Municipalidad de Ushuaia"},
    }

    junin_response = handle_tramites(msg, junin_ctx)
    ushuaia_response = handle_tramites(msg, ushuaia_ctx)
    cached_junin_response = handle_tramites(msg, junin_ctx)

    assert "junin.example" in junin_response["message_body"]
    assert "ushuaia.example" in ushuaia_response["message_body"]
    assert cached_junin_response == junin_response
    assert mock_google_search.call_args_list == [
        (("tramite licencia de conducir en Municipalidad de Junín",), {}),
        (("tramite licencia de conducir en Municipalidad de Ushuaia",), {}),
    ]


@pytest.mark.legacy
@patch('services.flows.tramites.google_search', return_value=[])
def test_tramites_without_tenant_identity_uses_neutral_query(mock_google_search):
    response = handle_tramites("certificado unico", {})

    assert "No encontré información" in response["message_body"]
    mock_google_search.assert_called_once_with("tramite certificado unico")
