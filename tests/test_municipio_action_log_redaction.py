import logging
from unittest.mock import patch

from services.actions.municipio_actions import (
    ActivarPanicoActionHandler,
    BuscarEstacionamientoActionHandler,
    ConsultarEstadoTicketActionHandler,
    ConsultarInfoTramiteActionHandler,
    ConsultarPuntosDeInteresActionHandler,
    CorregirDatosReclamoActionHandler,
    DerivarHumanoActionHandler,
    HacerSugerenciaActionHandler,
    ProcesarAdjuntoReclamoActionHandler,
)
from services.constants import CONTEXTO_MUNICIPIO


_SENSITIVE_VALUES = {
    "ubicacion": "CALLE_CIUDADANA_CANARY_742",
    "coordenadas": {"lat": "LAT_CANARY", "lon": "LON_CANARY"},
    "descripcion": "MENSAJE_CIUDADANO_CANARY",
    "dni": "32877851_CANARY",
    "email": "vecino-canary@example.invalid",
    "telefono": "+549261_CANARY",
    "archivo_url": "https://media.canary.invalid/private?token=SECRET_CANARY",
    "nuevo_valor": "NUEVO_VALOR_CIUDADANO_CANARY",
    "token": "bearer_CANARY_secret",
}


def _assert_no_sensitive_values_in_municipal_logs(caplog):
    emitted = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == "services.actions.municipio_actions"
    )
    assert "Executing municipal action" in emitted
    assert "supplied_fields=" in emitted
    assert "other_field_count=" in emitted

    flattened_canaries = [
        _SENSITIVE_VALUES["ubicacion"],
        _SENSITIVE_VALUES["coordenadas"]["lat"],
        _SENSITIVE_VALUES["coordenadas"]["lon"],
        _SENSITIVE_VALUES["descripcion"],
        _SENSITIVE_VALUES["dni"],
        _SENSITIVE_VALUES["email"],
        _SENSITIVE_VALUES["telefono"],
        _SENSITIVE_VALUES["archivo_url"],
        _SENSITIVE_VALUES["nuevo_valor"],
        _SENSITIVE_VALUES["token"],
    ]
    for canary in flattened_canaries:
        assert canary not in emitted


def test_municipal_action_entry_logs_only_safe_metadata(caplog):
    caplog.set_level(logging.INFO, logger="services.actions.municipio_actions")

    # Exercise every former full-action-data logging site.  Each path either
    # returns before I/O or has its ticket write replaced with a local stub.
    BuscarEstacionamientoActionHandler({CONTEXTO_MUNICIPIO: {}}).execute(
        {"email": _SENSITIVE_VALUES["email"], "token": _SENSITIVE_VALUES["token"]}
    )
    ConsultarEstadoTicketActionHandler({}).execute(
        {"dni": _SENSITIVE_VALUES["dni"], "token": _SENSITIVE_VALUES["token"]}
    )
    ConsultarInfoTramiteActionHandler({}).execute(
        {"email": _SENSITIVE_VALUES["email"], "token": _SENSITIVE_VALUES["token"]}
    )
    ConsultarPuntosDeInteresActionHandler({}).execute(
        {"telefono": _SENSITIVE_VALUES["telefono"], "token": _SENSITIVE_VALUES["token"]}
    )
    HacerSugerenciaActionHandler({}).execute(
        {"email": _SENSITIVE_VALUES["email"], "token": _SENSITIVE_VALUES["token"]}
    )
    ActivarPanicoActionHandler({}).execute(
        {
            "ubicacion": _SENSITIVE_VALUES["ubicacion"],
            "coordenadas": _SENSITIVE_VALUES["coordenadas"],
            "descripcion": _SENSITIVE_VALUES["descripcion"],
        }
    )
    ProcesarAdjuntoReclamoActionHandler({}).execute(
        {
            "archivo_url": _SENSITIVE_VALUES["archivo_url"],
            "analisis_imagen": {"texto_ocr": _SENSITIVE_VALUES["descripcion"]},
        }
    )
    CorregirDatosReclamoActionHandler({}).execute(
        {"nuevo_valor": _SENSITIVE_VALUES["nuevo_valor"], "token": _SENSITIVE_VALUES["token"]}
    )
    with patch(
        "services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket",
        side_effect=RuntimeError(_SENSITIVE_VALUES["archivo_url"]),
    ):
        DerivarHumanoActionHandler({}).execute(
            {
                "descripcion": _SENSITIVE_VALUES["descripcion"],
                "dni": _SENSITIVE_VALUES["dni"],
                "email": _SENSITIVE_VALUES["email"],
                "telefono": _SENSITIVE_VALUES["telefono"],
                "token": _SENSITIVE_VALUES["token"],
            }
        )

    _assert_no_sensitive_values_in_municipal_logs(caplog)

    emitted = "\n".join(record.getMessage() for record in caplog.records)
    assert "has_location=True" in emitted
    assert "has_coordinates=True" in emitted
    assert "has_attachment=True" in emitted
    # Unknown field names are counted, not interpolated into the log.
    assert "token" not in emitted
