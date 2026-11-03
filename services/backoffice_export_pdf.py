"""Institutional presentation only; no queries, authorization or file storage."""
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape


FIELD_LABELS = {
    "id": "Identificador", "number": "Número", "title": "Título", "name": "Nombre",
    "label": "Nombre", "description": "Descripción", "status": "Estado", "area": "Área",
    "channel": "Canal", "created_at": "Fecha de creación", "updated_at": "Última actualización",
    "priority": "Prioridad", "assigned_agent": "Responsable", "customer": "Cliente",
    "total": "Total registrado", "email": "Correo electrónico", "phone": "Teléfono",
    "role": "Rol", "channels": "Canales", "sources": "Procedencias",
    "records": "Registros asociados", "accepts_marketing": "Acepta comunicaciones",
    "categories_covered": "Categorías cubiertas", "channels_covered": "Canales cubiertos",
    "editable_scope": "Alcance editable", "workload": "Carga de trabajo",
    "source_model": "Modelo de origen", "record_key": "Referencia única",
    "request_id": "Referencia de exportación", "detail_endpoint": "Ruta de consulta",
    "allowed_actions": "Acciones disponibles", "sla_status": "Estado del plazo",
    "key": "Referencia del contacto", "type": "Tipo", "categories": "Categorías",
    "tenant_id": "Organización", "permissions": "Permisos", "zones": "Zonas",
}
SECONDARY_FIELDS = frozenset({
    "id", "source_model", "record_key", "request_id", "detail_endpoint", "allowed_actions",
    "key", "type", "editable_scope",
})


def field_label(field: str) -> str:
    return FIELD_LABELS.get(field, field.replace("_", " ").capitalize())


def field_value(field: str, value: Any) -> str:
    """Human display only; never infer a timezone or change stored data."""
    if value is None:
        return "Sin dato"
    if isinstance(value, bool):
        return "Sí" if value else "No"
    if field in {"created_at", "updated_at"} and isinstance(value, (str, datetime)):
        try:
            stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return str(value)
        if isinstance(value, str) and len(value) == 10:
            return stamp.strftime("%d/%m/%Y") + " (hora y zona no informadas)"
        result = stamp.strftime("%d/%m/%Y %H:%M:%S")
        if stamp.microsecond:
            result += "." + f"{stamp.microsecond:06d}".rstrip("0")
        offset = stamp.utcoffset()
        if offset is None:
            return result + " (zona no informada)"
        seconds = int(offset.total_seconds())
        hours, rest = divmod(abs(seconds), 3600)
        minutes, seconds_part = divmod(rest, 60)
        zone = f"UTC{'+' if seconds >= 0 else '-'}{hours:02d}:{minutes:02d}"
        if seconds_part:
            zone += f":{seconds_part:02d}"
        return result + " " + zone
    if isinstance(value, dict):
        return "\n".join(f"{field_label(str(key))}: {field_value(str(key), item)}"
                         for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))) or "Sin elementos"
    if isinstance(value, (list, tuple, set)):
        values = sorted(value, key=str) if isinstance(value, set) else value
        return "\n".join("- " + field_value(field, item) for item in values) or "Sin elementos"
    return str(value) if value != "" else "Sin dato (vacío)"


def render_backoffice_export_pdf(output, title: str, rows: list[dict[str, Any]]) -> None:
    import reportlab
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import KeepTogether, LongTable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, TableStyle

    font, bold = "BackofficeVera", "BackofficeVeraBold"
    font_root = Path(reportlab.__file__).parent / "fonts"
    for name, filename in ((font, "Vera.ttf"), (bold, "VeraBd.ttf")):
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(font_root / filename)))
    pdfmetrics.registerFontFamily(font, normal=font, bold=bold, italic=font, boldItalic=bold)
    navy, blue, muted = colors.HexColor("#16324f"), colors.HexColor("#176280"), colors.HexColor("#4d6276")
    border, pale = colors.HexColor("#dbe5ec"), colors.HexColor("#f4f7fa")
    body = ParagraphStyle("ExportBody", fontName=font, fontSize=8.6, leading=12, textColor=navy)
    field_style = ParagraphStyle("ExportField", parent=body, fontName=bold, fontSize=8, textColor=muted)
    heading = ParagraphStyle("ExportRecord", parent=body, fontName=bold, fontSize=11.5,
                             leading=16, spaceBefore=9, spaceAfter=7, keepWithNext=True)
    section = ParagraphStyle("ExportSection", parent=field_style, fontSize=7.5, leading=11,
                             spaceBefore=7, spaceAfter=4, keepWithNext=True, textColor=blue)
    document_title = ParagraphStyle("ExportTitle", parent=heading, fontSize=19, leading=25, spaceAfter=10)
    note = ParagraphStyle("ExportNote", parent=body, fontSize=8, leading=12, textColor=muted, spaceAfter=10)
    display_title = title
    for resource, label in (("tickets", "Casos"), ("orders", "Pedidos"), ("contacts", "Contactos"), ("team", "Equipo")):
        display_title = display_title.replace(f"Exportación de {resource}", f"Informe de {label}", 1)
    width, height = A4
    margin = 42
    usable = width - 2 * margin

    def paragraph(value: Any, style=body):
        return Paragraph(escape(str(value)).replace("\n", "<br/>"), style)

    def field_table(row: dict[str, Any], fields: list[str], secondary=False):
        cells = [[paragraph(field_label(field), field_style), paragraph(field_value(field, row[field]))]
                 for field in fields]
        table = LongTable(cells, colWidths=[124, usable - 124], splitByRow=1, splitInRow=1)
        table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
            ("LINEBELOW", (0, 0), (-1, -1), .35, border),
            ("BACKGROUND", (0, 0), (-1, -1), pale if secondary else colors.white),
        ]))
        return table

    story = [paragraph(display_title, document_title), paragraph(f"{len(rows)} registros incluidos", heading),
             paragraph("Información operativa del alcance seleccionado. No incluye archivos adjuntos ni el historial completo.", note)]
    preferred = ("number", "status", "area", "channel", "created_at", "updated_at", "description",
                 "customer", "total", "email", "phone", "role", "channels", "sources")
    for index, row in enumerate(rows, 1):
        title_field = next((key for key in ("title", "name", "label", "number", "id") if row.get(key) not in (None, "")), None)
        label = row[title_field] if title_field else "Registro sin título"
        fields = [key for key in preferred if key in row and key != title_field]
        fields.extend(key for key in sorted(row) if key not in fields and key != title_field)
        primary = [key for key in fields if key not in SECONDARY_FIELDS]
        secondary = [key for key in fields if key in SECONDARY_FIELDS]
        card = [paragraph(f"{index:02d}. {label}", heading)]
        if primary:
            card.append(field_table(row, primary))
        if secondary:
            card.extend([paragraph("REFERENCIAS Y DATOS COMPLEMENTARIOS", section), field_table(row, secondary, True)])
        card.append(Spacer(1, 10))
        # Short records move as a unit. Oversized records may split, including
        # a long text cell, so the report never drops the remaining data.
        story.append(KeepTogether(card))
    fields = sorted({field for row in rows for field in row})
    if fields:
        story.extend([PageBreak(), paragraph("Diccionario de campos", document_title),
                      paragraph("Correspondencia entre las etiquetas del informe y los campos de origen. Las fechas conservan la zona informada; una zona ausente se señala expresamente.", note)])
        dictionary = LongTable([[paragraph("Etiqueta del informe", field_style), paragraph("Campo de origen", field_style)]] +
                               [[paragraph(field_label(field)), paragraph(field)] for field in fields],
                               colWidths=[usable * .52, usable * .48], repeatRows=1, splitInRow=1)
        dictionary.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (-1, 0), pale),
            ("LINEBELOW", (0, 0), (-1, -1), .35, border),
            ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(dictionary)
    else:
        story.append(paragraph("No hay registros para el alcance y los filtros seleccionados.", body))

    def page_header(canvas, document):
        canvas.saveState()
        canvas.setFont(bold, 8)
        canvas.setFillColor(blue)
        canvas.drawString(margin, height - 29, "INFORME OPERATIVO")
        canvas.setFont(font, 7.5)
        canvas.setFillColor(muted)
        canvas.drawRightString(width - margin, height - 29, "Consulta de gestión")
        canvas.setStrokeColor(border)
        canvas.line(margin, height - 39, width - margin, height - 39)
        canvas.line(margin, 37, width - margin, 37)
        canvas.drawString(margin, 24, "Datos operativos - referencias de origen conservadas")
        canvas.drawRightString(width - margin, 24, f"Página {document.page}")
        canvas.restoreState()

    document = SimpleDocTemplate(output, pagesize=A4, leftMargin=margin, rightMargin=margin,
                                 topMargin=58, bottomMargin=51, title=display_title)
    document.build(story, onFirstPage=page_header, onLaterPages=page_header)
