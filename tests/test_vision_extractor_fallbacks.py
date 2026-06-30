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
    monkeypatch.setattr(vision_extractor, "_client", lambda: (_ for _ in ()).throw(AssertionError("legacy not expected")))

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
    monkeypatch.setattr(vision_extractor, "_client", lambda: (_ for _ in ()).throw(AssertionError("legacy not expected")))

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
    monkeypatch.setattr(vision_extractor, "_client", lambda: (_ for _ in ()).throw(AssertionError("legacy not expected")))

    rows = vision_extractor.extract_table_from_file(b"%PDF-1.7\n2 chapas galvanizadas", "Extrae productos")

    assert rows == [{"nombre": "chapas galvanizadas", "cantidad": 2}]
    assert "2 chapas galvanizadas" in captured["text"]
