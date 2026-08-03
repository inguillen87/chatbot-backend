import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from services import llm_utils


PII_CANARIES = (
    "+5492615550199",
    "32877851",
    "privado.canary@example.com",
    "Don Bosco 56 esquina Sarmiento",
    "PIN-739184",
    "https://private.example.test/claim?token=secret-canary",
)
PII_TEXT = (
    "Telefono +5492615550199; DNI 32877851; email privado.canary@example.com; "
    "direccion Don Bosco 56 esquina Sarmiento; PIN-739184; "
    "https://private.example.test/claim?token=secret-canary"
)


def _assert_log_is_pii_free(caplog):
    rendered = caplog.text
    for canary in PII_CANARIES:
        assert canary not in rendered


@pytest.mark.parametrize(
    ("extractor", "fields", "prefix"),
    [
        (
            llm_utils.extract_multiple_contact_details_llm,
            ["telefono_cliente", "dni_cliente", "email_cliente", "direccion_cliente"],
            "LLM_CONTACT_EXTRACT",
        ),
        (llm_utils.extract_complaint_details_llm, None, "LLM_COMPLAINT_EXTRACT"),
    ],
)
@pytest.mark.parametrize(
    ("provider_response", "expected_marker"),
    [
        (json.dumps([PII_TEXT]), "response_type=list"),
        ("", "LLM returned empty response"),
        ("   ", "response was empty after cleaning"),
    ],
)
def test_extraction_diagnostic_logs_never_include_citizen_pii(
    caplog,
    extractor,
    fields,
    prefix,
    provider_response,
    expected_marker,
):
    caplog.set_level(logging.INFO, logger="services.llm_utils")

    with patch("services.llm_utils.robust_chat", return_value=provider_response):
        if fields is None:
            result = extractor(PII_TEXT)
        else:
            result = extractor(PII_TEXT, fields)

    assert isinstance(result, dict)
    assert prefix in caplog.text
    assert expected_marker in caplog.text
    assert f"input_length={len(PII_TEXT)}" in caplog.text
    _assert_log_is_pii_free(caplog)


def test_order_extraction_logs_only_counts_and_types(caplog):
    caplog.set_level(logging.INFO, logger="services.llm_utils")
    provider_payload = json.dumps(
        [{"nombre_producto_ocr": PII_TEXT, "cantidad_ocr": 1}]
    )

    with patch(
        "services.llm_bridge.llamar_llm_para_generacion_texto",
        return_value=provider_payload,
    ):
        result = llm_utils.extraer_lista_pedido_de_texto_con_llm(PII_TEXT)

    assert result == [{"nombre_producto_ocr": PII_TEXT, "cantidad_ocr": 1}]
    assert "valid_item_count=1" in caplog.text
    _assert_log_is_pii_free(caplog)


def test_product_summary_logs_preserve_result_without_payload(caplog):
    caplog.set_level(logging.INFO, logger="services.llm_utils")
    original = f"{PII_TEXT} " + ("detalle " * 40)
    summary = f"{PII_TEXT} resumen operativo"

    with patch("services.llm_utils.robust_chat", return_value=summary):
        result = llm_utils.resumir_descripcion_producto_llm(
            original,
            max_longitud=220,
            min_longitud=10,
        )

    assert result == summary
    assert f"original_length={len(original)}" in caplog.text
    assert f"summary_length={len(summary)}" in caplog.text
    _assert_log_is_pii_free(caplog)


def test_vision_provider_error_log_uses_error_type_only(caplog, monkeypatch):
    caplog.set_level(logging.ERROR, logger="services.llm_utils")
    client = MagicMock()
    client.text_detection.side_effect = RuntimeError(PII_TEXT)
    fake_vision = SimpleNamespace(Image=lambda **kwargs: kwargs)
    monkeypatch.setattr(llm_utils, "vision", fake_vision)
    monkeypatch.setattr(llm_utils, "VISION_CLIENT", client)

    assert llm_utils.analyze_image_with_google_vision_ocr(b"image") == ""

    assert "error_type=RuntimeError" in caplog.text
    _assert_log_is_pii_free(caplog)


def test_image_description_error_log_preserves_raw_fallback_without_logging_it(caplog):
    caplog.set_level(logging.ERROR, logger="services.llm_utils")

    with patch(
        "services.llm_bridge.llamar_llm_para_generacion_texto",
        side_effect=RuntimeError(PII_TEXT),
    ):
        result = llm_utils.generar_descripcion_natural_de_imagen(PII_TEXT)

    assert result == PII_TEXT
    assert "error_type=RuntimeError" in caplog.text
    assert f"input_length={len(PII_TEXT)}" in caplog.text
    _assert_log_is_pii_free(caplog)


def test_summary_provider_error_log_preserves_truncated_fallback(caplog):
    caplog.set_level(logging.ERROR, logger="services.llm_utils")
    original = f"{PII_TEXT} " + ("detalle " * 40)

    with patch(
        "services.llm_utils.robust_chat",
        side_effect=RuntimeError(PII_TEXT),
    ):
        result = llm_utils.resumir_descripcion_producto_llm(
            original,
            max_longitud=220,
            min_longitud=10,
        )

    assert result == original[:220].strip()
    assert "error_type=RuntimeError" in caplog.text
    assert f"original_length={len(original)}" in caplog.text
    _assert_log_is_pii_free(caplog)


def test_document_ai_configuration_and_error_logs_are_pii_free(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="services.llm_utils")

    class FakeDocumentAI:
        class DocumentProcessorServiceClient:
            def __init__(self, *args, **kwargs):
                raise RuntimeError(PII_TEXT)

        class RawDocument:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class ProcessRequest:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

    monkeypatch.setattr(llm_utils, "documentai", FakeDocumentAI)

    result = llm_utils.analyze_document_with_google_document_ai(
        project_id=PII_TEXT,
        location="",
        processor_id=PII_TEXT,
        file_content=b"document",
        mime_type=PII_TEXT,
    )

    assert result is None
    assert "has_project=True" in caplog.text
    assert "has_location=False" in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    _assert_log_is_pii_free(caplog)
