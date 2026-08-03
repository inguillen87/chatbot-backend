import json
import logging
import os
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests
from flask import Flask

from services import herramientas_municipio as hm
from services import google_maps_service as gms
from services import location_service as ls


ADDRESS_CANARY = "DON BOSCO CANARY 56, JUNIN CANARY"
QUERY_CANARY = "FARMACIA_QUERY_CANARY"
PROVIDER_CANARY = "https://provider.invalid/body?token=PROVIDER_SECRET_CANARY"
USER_CANARY = "USER_SECRET_CANARY"
LAT_CANARY = -34.581234567
LON_CANARY = -60.941234567


class _ExplodingOpenAIClient:
    @property
    def chat(self):
        raise AssertionError("OpenAI must not be accessed while test networking is disabled")


def _geo_logs(caplog) -> str:
    return "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name in {"services.herramientas_municipio", "services.location_service"}
    )


@pytest.mark.parametrize("testing_source", ["app", "environment"])
def test_parse_direccion_blocks_openai_network_in_tests_and_uses_fallback(
    caplog,
    testing_source,
):
    caplog.set_level(logging.INFO, logger="services.herramientas_municipio")
    app = Flask(__name__)
    app.config["TESTING"] = testing_source == "app"
    app_context = app.app_context() if testing_source == "app" else nullcontext()
    testing_env = "1" if testing_source == "environment" else "0"

    with (
        app_context,
        patch.dict(
            os.environ,
            {
                "TESTING": testing_env,
                "OPENAI_ALLOW_NETWORK_IN_TESTS": "0",
            },
            clear=False,
        ),
        patch.object(hm, "openai_client", _ExplodingOpenAIClient()),
    ):
        result = hm.parse_direccion_completa(
            ADDRESS_CANARY,
            {"ciudad": "JUNIN_DEFAULT_CANARY", "provincia": "MENDOZA_DEFAULT_CANARY"},
        )

    assert result is not None
    assert result["calle"] == "DON BOSCO CANARY"
    emitted = _geo_logs(caplog)
    assert "reason=test_network_disabled" in emitted
    assert "result_fields=" in emitted
    for canary in (
        ADDRESS_CANARY,
        "DON BOSCO CANARY",
        "JUNIN CANARY",
        "JUNIN_DEFAULT_CANARY",
        "MENDOZA_DEFAULT_CANARY",
    ):
        assert canary not in emitted


def test_parse_direccion_test_network_opt_in_uses_only_mocked_openai(caplog):
    caplog.set_level(logging.INFO, logger="services.herramientas_municipio")
    app = Flask(__name__)
    app.config["TESTING"] = True
    client = MagicMock()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=json.dumps(
                        {
                            "calle": "LLM_STREET_CANARY",
                            "numero": "99_CANARY",
                            "localidad": "LLM_CITY_CANARY",
                        }
                    )
                )
            )
        ]
    )

    with (
        app.app_context(),
        patch.dict(
            os.environ,
            {"TESTING": "1", "OPENAI_ALLOW_NETWORK_IN_TESTS": "1"},
            clear=False,
        ),
        patch.object(hm, "openai_client", client),
    ):
        result = hm.parse_direccion_completa(ADDRESS_CANARY, {"ciudad": "Junin"})

    client.chat.completions.create.assert_called_once()
    assert result["calle"] == "LLM_STREET_CANARY"
    emitted = _geo_logs(caplog)
    assert "Address normalized" in emitted
    for canary in (ADDRESS_CANARY, "LLM_STREET_CANARY", "99_CANARY", "LLM_CITY_CANARY"):
        assert canary not in emitted


def test_location_service_failures_log_only_safe_metadata(caplog):
    caplog.set_level(logging.ERROR, logger="services.location_service")
    gmaps = MagicMock()
    provider_failure = RuntimeError(PROVIDER_CANARY)
    gmaps.geocode.side_effect = provider_failure
    gmaps.places_autocomplete.side_effect = provider_failure
    gmaps.places_nearby.side_effect = provider_failure

    with patch.object(ls, "get_gmaps_client", return_value=gmaps):
        assert ls.geocode_address(ADDRESS_CANARY, {"country": "AR"}) is None
        assert ls.autocomplete_address(ADDRESS_CANARY, {"country": "AR"}) is None
        assert (
            ls.find_nearby_places(
                {"lat": LAT_CANARY, "lng": LON_CANARY},
                QUERY_CANARY,
            )
            is None
        )

    emitted = _geo_logs(caplog)
    assert "error_type=RuntimeError" in emitted
    assert "input_chars=" in emitted
    assert "has_location=True" in emitted
    for canary in (
        ADDRESS_CANARY,
        QUERY_CANARY,
        PROVIDER_CANARY,
        str(LAT_CANARY),
        str(LON_CANARY),
    ):
        assert canary not in emitted


def test_geopy_provider_failures_do_not_log_addresses_coordinates_or_bodies(caplog):
    caplog.set_level(logging.INFO, logger="services.google_maps_service")
    geolocator = MagicMock()
    geolocator.geocode.side_effect = RuntimeError(PROVIDER_CANARY)
    geolocator.reverse.side_effect = RuntimeError(PROVIDER_CANARY)

    with patch.object(gms, "_get_geolocators", return_value=[geolocator]):
        assert gms.get_coordinates(ADDRESS_CANARY) is None
        assert gms.obtener_direccion_de_coordenadas(LAT_CANARY, LON_CANARY) is None

    emitted = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == "services.google_maps_service"
    )
    assert "error_type=RuntimeError" in emitted
    assert "input_chars=" in emitted
    assert "has_coordinates=True" in emitted
    for canary in (
        ADDRESS_CANARY,
        PROVIDER_CANARY,
        str(LAT_CANARY),
        str(LON_CANARY),
    ):
        assert canary not in emitted


def test_geo_tools_redact_queries_coordinates_and_provider_bodies(caplog):
    caplog.set_level(logging.INFO, logger="services.herramientas_municipio")
    request_failure = requests.exceptions.RequestException(PROVIDER_CANARY)

    with (
        patch.object(hm, "Maps_API_KEY", "mock-key"),
        patch.object(hm.requests, "get", side_effect=request_failure),
    ):
        hm.consultar_recoleccion_por_direccion(
            ADDRESS_CANARY,
            {
                "municipio_config_actual": {
                    "ciudad": "CITY_COMPONENT_CANARY",
                    "provincia": "STATE_COMPONENT_CANARY",
                }
            },
        )
        hm.buscar_puntos_de_interes(
            rubro=QUERY_CANARY,
            localidad=ADDRESS_CANARY,
        )

    provider_response = MagicMock()
    provider_response.raise_for_status.return_value = None
    provider_response.json.return_value = {
        "status": "PROVIDER_STATUS_CANARY",
        "error_message": PROVIDER_CANARY,
        "results": [],
    }
    with (
        patch.dict(os.environ, {"GEOCODING_ALLOW_NETWORK_IN_TESTS": "1"}),
        patch.object(hm, "Maps_API_KEY", "mock-key"),
        patch.object(hm, "geocodificar_inversa_llm", return_value=None),
        patch.object(hm, "_reverse_geocode_with_geopy", return_value=None),
        patch.object(hm.requests, "get", return_value=provider_response),
    ):
        assert hm.obtener_direccion_de_coordenadas(LAT_CANARY, LON_CANARY) is None

    hm.log_uso_herramienta(
        "GEO_TOOL_CANARY",
        USER_CANARY,
        {
            "direccion": ADDRESS_CANARY,
            PROVIDER_CANARY: "arbitrary-key-value",
        },
        PROVIDER_CANARY,
    )

    emitted = _geo_logs(caplog)
    assert "input_chars=" in emitted
    assert "has_coordinates=True" in emitted
    assert "parameter_fields=['direccion']" in emitted
    assert "other_parameter_count=1" in emitted
    for canary in (
        ADDRESS_CANARY,
        QUERY_CANARY,
        PROVIDER_CANARY,
        USER_CANARY,
        "GEO_TOOL_CANARY",
        "CITY_COMPONENT_CANARY",
        "STATE_COMPONENT_CANARY",
        "PROVIDER_STATUS_CANARY",
        str(LAT_CANARY),
        str(LON_CANARY),
    ):
        assert canary not in emitted
