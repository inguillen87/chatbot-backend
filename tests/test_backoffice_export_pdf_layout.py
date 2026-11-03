"""PDF presentation only; synthetic rows, actual ReportLab and PDFium.

The HTTP/auth/selection contracts have their separate source-bound coverage.
"""
import pypdfium2 as pdfium

from routes.backoffice import _export_bytes


def _pages(data):
    document = pdfium.PdfDocument(data)
    try:
        texts = []
        for index in range(len(document)):
            page = document[index]
            text = page.get_textpage()
            try:
                texts.append(text.get_text_bounded())
            finally:
                text.close()
                page.close()
        return texts
    finally:
        document.close()


def _compact(text):
    return " ".join(text.split())


def _records(count=31):
    return [{
        "id": index, "title": f"FICHA-{index:02d} Atención pública",
        "status": "Resuelto" if index % 2 else "En proceso", "area": "Arbolado",
        "channel": "WhatsApp", "created_at": "2026-11-03T09:10:11-03:00",
        "description": "Revisión sintética de la situación y su seguimiento.",
        "record_key": f"TenantTicket:REF-{index:02d}-FIN", "source_model": "TenantTicket",
    } for index in range(1, count + 1)]


def test_all_31_short_records_keep_their_reference_on_the_same_page_and_paginate():
    pages = _pages(_export_bytes("pdf", "Exportación de tickets · organización", _records()))
    assert len(pages) > 1
    for index in range(1, 32):
        title_pages = [page for page, text in enumerate(pages) if f"FICHA-{index:02d} Atención pública" in text]
        reference_pages = [page for page, text in enumerate(pages) if f"TenantTicket:REF-{index:02d}-FIN" in text]
        assert len(title_pages) == 1 and title_pages == reference_pages
    for index, text in enumerate(pages, 1):
        assert "INFORME OPERATIVO" in text and f"Página {index}" in text
    joined = "\n".join(pages)
    assert "31 registros incluidos" in joined and "FICHA-31 Atención pública" in joined
    assert "Diccionario de campos" in joined and "record_key" in joined


def test_resource_titles_and_labels_are_spanish_without_repeating_the_record_title():
    for resource, label in (("tickets", "Casos"), ("orders", "Pedidos"), ("contacts", "Contactos"), ("team", "Equipo")):
        text = "\n".join(_pages(_export_bytes("pdf", f"Exportación de {resource} · alcance", _records(1))))
        assert f"Informe de {label}" in text
        assert text.count("FICHA-01 Atención pública") == 1
        body = text.split("Diccionario de campos", 1)[0]
        assert "Fecha de creación" in body and "Referencia única" in body
        assert "created_at" not in body and "record_key" not in body
        assert "created_at" in text and "record_key" in text


def test_dates_preserve_offset_precision_and_explicitly_declare_missing_timezone():
    rows = [{"title": "Fecha con zona", "created_at": "2026-11-03T09:10:11.123456-03:00",
             "updated_at": "2026-11-03T12:13:14"},
            {"title": "Fecha sin hora", "created_at": "2026-11-04"},
            {"title": "Fecha no normalizable", "created_at": "VALOR-ORIGINAL-DE-FECHA"}]
    text = _compact("\n".join(_pages(_export_bytes("pdf", "Fechas de muestra", rows))))
    assert "03/11/2026 09:10:11.123456 UTC-03:00" in text
    assert "03/11/2026 12:13:14 (zona no informada)" in text
    assert "04/11/2026 (hora y zona no informadas)" in text
    assert "VALOR-ORIGINAL-DE-FECHA" in text
    assert "2026-11-03T" not in text


def test_markup_unicode_and_nested_values_remain_present_and_traceable():
    row = {"title": "Atención Ñandú & <control>", "description": "DESCRIPCIÓN-COMPLETA <b>literal</b> & válida",
           "email": "SYNTHETIC@example.invalid", "source_model": "TenantTicket", "record_key": "TenantTicket:42",
           "editable_scope": {"categories": ["CATEGORÍA-UNO", "CATEGORÍA-DOS"], "tenant_id": 42},
           "accepts_marketing": False, "custom_marker": "DATO-NO-CATALOGADO"}
    text = _compact("\n".join(_pages(_export_bytes("pdf", "Muestra <sin marca>", [row]))))
    for marker in ("Atención Ñandú & <control>", "DESCRIPCIÓN-COMPLETA <b>literal</b> & válida",
                   "SYNTHETIC@example.invalid", "TenantTicket:42", "CATEGORÍA-UNO", "CATEGORÍA-DOS", "DATO-NO-CATALOGADO"):
        assert marker in text
    assert "Acepta comunicaciones" in text and "No" in text
    assert "custom_marker" in text and "editable_scope" in text


def test_a_record_larger_than_one_page_splits_without_losing_the_end_or_pagination():
    body = " ".join(f"PALABRA-{index:04d}" for index in range(1400))
    row = {"title": "FICHA-LARGA-INICIO", "description": body + " FIN-DESCRIPCIÓN-LARGA",
           "record_key": "TenantTicket:FIN-DE-FICHA-LARGA", "source_model": "TenantTicket"}
    pages = _pages(_export_bytes("pdf", "Texto extenso de muestra", [row]))
    assert len(pages) > 2
    text = "\n".join(pages)
    assert text.count("FICHA-LARGA-INICIO") == 1
    assert "PALABRA-0000" in text and "PALABRA-1399" in text
    assert "FIN-DESCRIPCIÓN-LARGA" in text and "TenantTicket:FIN-DE-FICHA-LARGA" in text
    for index, page in enumerate(pages, 1):
        assert "INFORME OPERATIVO" in page and f"Página {index}" in page


def test_an_empty_pdf_reports_zero_without_inventing_a_case_or_currency():
    text = "\n".join(_pages(_export_bytes("pdf", "Exportación de orders · alcance", [])))
    assert "Informe de Pedidos" in text and "0 registros incluidos" in text
    assert "No hay registros" in text and "Página 1" in text
    assert "ARS" not in text and "$" not in text
