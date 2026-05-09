from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "demo_catalogs"


CATALOGS = {
    "colegios": {
        "title": "Chatboc para Colegios",
        "subtitle": "Atencion escolar con IA para familias, alumnos y secretaria.",
        "accent": colors.HexColor("#2563EB"),
        "items": [
            ("Asistencia inteligente", "Justificacion de inasistencias con audio, foto y archivo", "$ 189.000 / mes"),
            ("Secretaria digital", "Consultas, certificados, comunicados y turnos internos", "$ 249.000 / mes"),
            ("Admisiones y leads", "Captura familias interesadas con seguimiento comercial", "$ 289.000 / mes"),
            ("Convivencia cuidada", "Derivacion responsable de casos sensibles al equipo humano", "Incluido"),
        ],
    },
    "gobiernos": {
        "title": "Chatboc para Gobiernos",
        "subtitle": "Mesa ciudadana para reclamos, tramites, mapas y seguimiento.",
        "accent": colors.HexColor("#0F9F6E"),
        "items": [
            ("Reclamos con ubicacion", "Casos por barrio, categoria, SLA y evidencia visual", "$ 249.000 / mes"),
            ("Tramites guiados", "Respuestas oficiales, requisitos y proximo paso", "$ 289.000 / mes"),
            ("Mapa operativo", "Zonas calientes, tickets vencidos y demanda por area", "$ 349.000 / mes"),
            ("Campanas y participacion", "Encuestas, votaciones y opinion ciudadana", "Modulo opcional"),
        ],
    },
    "empresas": {
        "title": "Chatboc para Empresas",
        "subtitle": "Ventas, soporte y pedidos desde widget, WhatsApp y panel.",
        "accent": colors.HexColor("#7C3AED"),
        "items": [
            ("Catalogo conversacional", "Productos, variantes, precios y promociones", "$ 169.000 / mes"),
            ("Pedidos y checkout", "Carrito guiado, datos de envio y pago preparado", "$ 249.000 / mes"),
            ("Lead scoring", "Detecta intencion alta y deriva a ventas con contexto", "$ 289.000 / mes"),
            ("Soporte multimodal", "Imagenes, audios, archivos y ubicacion en un solo hilo", "Incluido"),
        ],
    },
}


def build_pdf(folder: Path, filename: str, profile: dict[str, object], *, price_list: bool = False) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "DemoTitle",
        parent=styles["Title"],
        textColor=profile["accent"],
        fontSize=25,
        leading=30,
        spaceAfter=8,
    )
    subtitle_style = ParagraphStyle(
        "DemoSubtitle",
        parent=styles["BodyText"],
        textColor=colors.HexColor("#374151"),
        fontSize=12,
        leading=17,
        spaceAfter=14,
    )
    body_style = ParagraphStyle(
        "DemoBody",
        parent=styles["BodyText"],
        textColor=colors.HexColor("#111827"),
        fontSize=10,
        leading=14,
    )
    doc = SimpleDocTemplate(str(path), pagesize=A4, rightMargin=18 * mm, leftMargin=18 * mm, topMargin=18 * mm, bottomMargin=18 * mm)
    story = [
        Paragraph(str(profile["title"]), title_style),
        Paragraph(str(profile["subtitle"]), subtitle_style),
        Paragraph("Documento demo para mostrar el recorrido comercial. Los valores son orientativos y pueden configurarse por tenant.", body_style),
        Spacer(1, 8 * mm),
    ]
    rows = [["Modulo", "Incluye", "Valor demo"]]
    for name, description, price in profile["items"]:
        rows.append([name, description, price if price_list else "Disponible en demo"])
    table = Table(rows, colWidths=[42 * mm, 92 * mm, 38 * mm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), profile["accent"]),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D1D5DB")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F9FAFB")]),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    story.append(table)
    story.append(Spacer(1, 10 * mm))
    story.append(Paragraph("Sugerencia de demo: abrir el widget, elegir pilar, enviar audio o imagen y convertir la conversacion en ticket, pedido o lead.", body_style))
    doc.build(story)


def main() -> None:
    for slug, profile in CATALOGS.items():
        folder = OUT / slug
        build_pdf(folder, f"catalogo-demo-{slug}.pdf", profile, price_list=False)
        build_pdf(folder, "lista-precios-demo.pdf", profile, price_list=True)


if __name__ == "__main__":
    main()
