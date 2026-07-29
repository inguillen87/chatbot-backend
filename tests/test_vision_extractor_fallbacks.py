import base64
from types import SimpleNamespace
from unittest.mock import MagicMock

from services import vision_extractor


def test_extract_table_from_image_structured_provider(monkeypatch):
    monkeypatch.setattr(
        vision_extractor,
        "analyze_image_structured",
        lambda image, prompt: {
            "columns": ["cantidad", "nombre", "unidad"],
            "rows": [[2, "Chapa galvanizada", "unidad"]],
        },
    )
    rows = vision_extractor.extract_table_from_file(b"\x89PNG\r\n\x1a\nimage", "Extrae productos")

    assert rows == [{"cantidad": 2, "nombre": "Chapa galvanizada", "unidad": "unidad"}]


def test_extract_table_from_image_uses_ocr_text_fallback(monkeypatch):
    monkeypatch.setattr(vision_extractor, "analyze_image_structured", lambda image, prompt: None)
    monkeypatch.setattr(
        vision_extractor,
        "analyze_image_smart",
        lambda image, prompt=None: {
            "labels": [],
            "objects": [],
            "full_text_annotation": {"description": "2 chapas galvanizadas\n1 caja de clavos"},
        },
    )
    monkeypatch.setattr(vision_extractor, "analyze_text_structured", lambda text, prompt: None)
    rows = vision_extractor.extract_table_from_file(b"\xff\xd8\xffimage", "Extrae productos")

    assert rows == [
        {"nombre": "chapas galvanizadas", "cantidad": "2"},
        {"nombre": "caja de clavos", "cantidad": "1"},
    ]


def test_pdf_text_uses_local_text_fallback(monkeypatch):
    captured = {}

    def fake_text_structured(text, prompt):
        captured["text"] = text
        return {"items": [{"nombre": "chapas galvanizadas", "cantidad": 2}]}

    monkeypatch.setattr(vision_extractor, "analyze_text_structured", fake_text_structured)
    rows = vision_extractor.extract_table_from_file(b"%PDF-1.7\n2 chapas galvanizadas", "Extrae productos")

    assert rows == [{"nombre": "chapas galvanizadas", "cantidad": 2}]
    assert "2 chapas galvanizadas" in captured["text"]


def test_binary_pdf_uses_responses_file_data_strict_schema(monkeypatch):
    pdf_bytes = b"%PDF-1.7\n\x00\x01\x02"
    client = MagicMock()
    client.responses.create.return_value = SimpleNamespace(
        output_text='{"columns":["cantidad","nombre"],"rows":[[2,"Chapas"]]}',
        output=[],
    )
    monkeypatch.setattr(vision_extractor, "_extract_with_project_fallbacks", lambda *_args: None)
    monkeypatch.setattr(vision_extractor, "_decode_text", lambda _data: None)
    monkeypatch.setattr(vision_extractor, "_decode_pdf_text", lambda _data: None)
    monkeypatch.setattr(
        vision_extractor.vision_fallback_service,
        "_get_openai_client",
        lambda: client,
    )
    monkeypatch.setenv("OPENAI_SAFETY_IDENTIFIER_SECRET", "test-secret")

    rows = vision_extractor.extract_table_from_file(
        pdf_bytes,
        "Extrae productos",
        model="gpt-5.6-sol",
    )

    assert rows == [{"cantidad": 2, "nombre": "Chapas"}]
    request = client.responses.create.call_args.kwargs
    assert request["model"] == "gpt-5.6-sol"
    assert request["store"] is False
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["text"]["format"]["strict"] is True
    file_part = request["input"][0]["content"][0]
    assert file_part["type"] == "input_file"
    assert file_part["filename"] == "document.pdf"
    assert file_part["detail"] == "auto"
    assert file_part["file_data"] == (
        "data:application/pdf;base64," + base64.b64encode(pdf_bytes).decode("ascii")
    )
    assert request["input"][0]["content"][1]["type"] == "input_text"
    assert len(request["safety_identifier"]) == 64
    assert request["safety_identifier"] not in {pdf_bytes.hex(), "test-secret"}


def test_image_is_not_resubmitted_as_file_after_image_pipeline(monkeypatch):
    image_bytes = b"\x89PNG\r\n\x1a\nimage"
    binary_fallback = MagicMock()
    monkeypatch.setattr(vision_extractor, "_extract_with_project_fallbacks", lambda *_args: None)
    monkeypatch.setattr(
        vision_extractor,
        "_extract_binary_file_with_openai",
        binary_fallback,
    )

    rows = vision_extractor.extract_table_from_file(image_bytes, "Extrae productos")

    assert rows is None
    binary_fallback.assert_not_called()
