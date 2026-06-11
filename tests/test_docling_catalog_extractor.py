from services.catalog_pipeline.extractors.docling_extractor import (
    DoclingExtractor,
    parse_markdown_tables,
)


def test_parse_markdown_tables_extracts_rows():
    markdown = """
| Producto | Precio |
| --- | --- |
| Malbec | 1000 |
| Cabernet | 1200 |
"""

    tables = parse_markdown_tables(markdown)

    assert len(tables) == 1
    assert tables[0].to_dict(orient="records")[0] == {"Producto": "Malbec", "Precio": "1000"}


def test_docling_extractor_normalizes_rows(monkeypatch):
    monkeypatch.setattr(
        "services.catalog_pipeline.extractors.docling_extractor.extract_document",
        lambda file_content, filename: {
            "status": "ok",
            "warnings": [],
            "markdown": "| Producto | Precio |\n| --- | --- |\n| Malbec | 1000 |",
            "metadata": {"filename": filename},
        },
    )

    result = DoclingExtractor().extract(b"data", "catalogo.pdf")

    assert result["confidence"] == 0.82
    assert result["rows"] == [{"Producto": "Malbec", "Precio": "1000"}]
    assert result["metadata"]["table_count"] == 1
