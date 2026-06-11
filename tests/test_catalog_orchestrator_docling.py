from services.catalog_pipeline.orchestrator import CatalogOrchestrator


def test_catalog_orchestrator_prefers_docling_for_pdf(monkeypatch):
    def fake_docling(self, file_content, filename, **kwargs):
        return {
            "columns": [{"key": "Producto", "label": "Producto", "type": "string"}],
            "rows": [{"Producto": "Malbec"}],
            "confidence": 0.82,
            "warnings": [],
            "metadata": {},
        }

    monkeypatch.setattr(
        "services.catalog_pipeline.orchestrator.DoclingExtractor.extract",
        fake_docling,
    )

    result = CatalogOrchestrator().process(b"%PDF", "catalogo.pdf")

    assert result["engine"] == "docling"
    assert result["rows"] == [{"Producto": "Malbec"}]


def test_catalog_orchestrator_falls_back_when_docling_empty(monkeypatch):
    monkeypatch.setattr(
        "services.catalog_pipeline.orchestrator.DoclingExtractor.extract",
        lambda self, file_content, filename, **kwargs: {
            "columns": [],
            "rows": [],
            "confidence": 0.0,
            "warnings": ["Docling disabled"],
            "metadata": {},
        },
    )
    monkeypatch.setattr(
        "services.catalog_pipeline.orchestrator.PDFTextExtractor.extract",
        lambda self, file_content, filename, **kwargs: {
            "columns": [{"key": "Producto", "label": "Producto", "type": "string"}],
            "rows": [{"Producto": "Cabernet"}],
            "confidence": 1.0,
            "warnings": [],
            "metadata": {},
        },
    )

    result = CatalogOrchestrator().process(b"%PDF", "catalogo.pdf")

    assert result["engine"] == "pdf_text"
    assert result["rows"] == [{"Producto": "Cabernet"}]
    assert "Docling disabled" in result["warnings"]
