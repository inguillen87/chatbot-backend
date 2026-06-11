from types import SimpleNamespace

from services import open_source_document_intelligence as docint


class FakeDocument:
    def export_to_markdown(self):
        return "| Producto | Precio |\n| --- | --- |\n| Malbec | 1000 |"

    def export_to_text(self):
        return "Producto Precio Malbec 1000"


class FakeConverter:
    def convert(self, path):
        return SimpleNamespace(document=FakeDocument())


def test_docling_disabled_returns_none(monkeypatch):
    monkeypatch.delenv("DOCLING_ENABLED", raising=False)

    assert docint.extract_document(b"data", "catalogo.pdf") is None


def test_docling_extracts_markdown_when_enabled(monkeypatch):
    monkeypatch.setenv("DOCLING_ENABLED", "true")
    monkeypatch.setattr(docint, "_get_docling_converter", lambda: FakeConverter())

    result = docint.extract_document(b"data", "catalogo.pdf")

    assert result["status"] == "ok"
    assert "Malbec" in result["markdown"]
    assert result["metadata"]["extension"] == ".pdf"


def test_docling_respects_max_file_size(monkeypatch):
    monkeypatch.setenv("DOCLING_ENABLED", "true")
    monkeypatch.setenv("DOCLING_MAX_FILE_MB", "1")

    result = docint.extract_document(b"x" * (2 * 1024 * 1024), "catalogo.pdf")

    assert result["status"] == "skipped"
    assert result["warnings"]
