"""Utility script to regenerate the curated demo PDFs for Cuatro Fincas Winery.

This script avoids third-party dependencies so it can run in constrained
CI environments. It emits minimalist yet well formatted PDF files using the
classic PDF text operators. Each document mirrors the curated resources used
by the demo: portfolio catalog, wholesale price list and corporate tastings.

Run it from the project root:

    python scripts/generate_bodega_demo_pdfs.py

The resulting files are written to ``static/demo/bodega``.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "static" / "demo" / "bodega"


@dataclass
class LineSpec:
    font: str
    size: int
    text: str
    leading: float | None = None
    dx: float = 0


def _pdf_escape(text: str) -> str:
    """Escape parentheses and backslashes for PDF literal strings."""

    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _build_text_stream(lines: Iterable[LineSpec], *, start_x: int = 72, start_y: int = 720) -> bytes:
    commands: List[str] = ["BT"]
    current_font: str | None = None
    current_size: int | None = None
    first_line = True

    for spec in lines:
        if spec.font != current_font or spec.size != current_size:
            commands.append(f"/{spec.font} {spec.size} Tf")
            current_font = spec.font
            current_size = spec.size

        escaped = _pdf_escape(spec.text)
        if first_line:
            commands.append(f"{start_x} {start_y} Td ({escaped}) Tj")
            first_line = False
        else:
            leading = spec.leading if spec.leading is not None else -18
            commands.append(f"{spec.dx} {leading} Td ({escaped}) Tj")

    commands.append("ET")
    stream_text = "\n".join(commands)
    return stream_text.encode("latin-1")


def _write_pdf(path: Path, text_stream: bytes, *, width: int = 612, height: int = 792) -> None:
    length = len(text_stream)
    objects: List[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] /Contents 4 0 R /Resources << /Font << /F1 5 0 R /F2 6 0 R /F3 7 0 R >> >> >>".encode(
            "latin-1"
        ),
        b"<< /Length %d >>\nstream\n" % length + text_stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Oblique >>",
    ]

    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("wb") as fh:
        fh.write(b"%PDF-1.4\n")
        offsets: List[int] = []

        for index, obj in enumerate(objects, start=1):
            offsets.append(fh.tell())
            fh.write(f"{index} 0 obj\n".encode("ascii"))
            fh.write(obj)
            fh.write(b"\nendobj\n")

        xref_offset = fh.tell()
        fh.write(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
        fh.write(b"0000000000 65535 f \n")
        for offset in offsets:
            fh.write(f"{offset:010d} 00000 n \n".encode("ascii"))

        fh.write(b"trailer\n")
        fh.write(f"<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode("ascii"))
        fh.write(b"startxref\n")
        fh.write(f"{xref_offset}\n".encode("ascii"))
        fh.write(b"%%EOF")


def build_catalog_pdf() -> None:
    lines = [
        LineSpec("F1", 26, "Cuatro Fincas Winery · Portfolio Premium 2024"),
        LineSpec("F2", 12, "Perdriel, Lujan de Cuyo · Director enologico: Salvador Patti", leading=-30),
        LineSpec("F1", 16, "Coleccion Signature", leading=-36),
        LineSpec(
            "F2",
            12,
            "- Gran Malbec Reserva 2021 · Parcelas seleccionadas de Vistalba y Agrelo, 14 meses roble frances.",
            leading=-20,
        ),
        LineSpec(
            "F2",
            12,
            "  Moras negras, casis y cacao. Botella $18.900 · Caja x6 $107.400 (10% off).",
            leading=-16,
        ),
        LineSpec(
            "F2",
            12,
            "- Cabernet Franc Single Vineyard 2022 · Hierbas andinas, grafito y cereza negra.",
            leading=-20,
        ),
        LineSpec(
            "F2",
            12,
            "  Crianza de 12 meses en toneles de 500 L. Botella $20.500 · Caja x6 $116.900.",
            leading=-16,
        ),
        LineSpec(
            "F2",
            12,
            "- Bonarda Organica Don Salvador 2023 · Vinas de 45 anos en pergola mendocina.",
            leading=-20,
        ),
        LineSpec(
            "F2",
            12,
            "  Frutos rojos maduros, anis y final especiado. Botella $13.200 · Caja x6 $74.800.",
            leading=-16,
        ),
        LineSpec(
            "F2",
            12,
            "- Chardonnay Reserva 2022 · Fermentacion en barrica y batonage sobre lias 8 meses.",
            leading=-20,
        ),
        LineSpec(
            "F2",
            12,
            "  Durazno blanco, miel y avellanas. Botella $15.800 · Caja x6 $89.400.",
            leading=-16,
        ),
        LineSpec(
            "F2",
            12,
            "- Extra Brut Pinot Noir - Chardonnay 2021 · Metodo tradicional 18 meses sobre lias.",
            leading=-20,
        ),
        LineSpec(
            "F2",
            12,
            "  Perlage delicado, brioche y fruta roja fresca. Botella $16.800 · Caja x6 $95.800.",
            leading=-16,
        ),
        LineSpec("F2", 12, "", leading=-18),
        LineSpec("F1", 16, "Experiencias y Hospitality", leading=-32),
        LineSpec(
            "F2",
            12,
            "- Degustaciones guiadas viernes y sabado 18 hs · Cupo 14 personas, reserva previa.",
            leading=-20,
        ),
        LineSpec(
            "F2",
            12,
            "  Incluye maridaje gourmet y recorrido por sala de barricas. Tarifa $18.000 por persona.",
            leading=-16,
        ),
        LineSpec(
            "F2",
            12,
            "- Atencion personalizada para hoteles boutique, restaurantes y eventos corporativos.",
            leading=-20,
        ),
        LineSpec(
            "F2",
            12,
            "  Packs institucionales con grabado laser, tarjetas dedicadas y envio refrigerado nacional.",
            leading=-16,
        ),
        LineSpec("F2", 12, "", leading=-18),
        LineSpec("F1", 16, "Contactos y Logistica", leading=-32),
        LineSpec(
            "F2",
            12,
            "- Envio bonificado en Gran Mendoza desde $45.000 · Andreani Wine para interior 48-72 hs.",
            leading=-20,
        ),
        LineSpec(
            "F2",
            12,
            "- Consultas comerciales: ventas@cuatrofincas.com · +54 9 261 555 0000.",
            leading=-16,
        ),
        LineSpec(
            "F3",
            11,
            "Servicio recomendado: 16 C para tintos, 10 C para blancos y espumosos. Decantar 45 minutos.",
            leading=-22,
        ),
    ]

    stream = _build_text_stream(lines)
    _write_pdf(OUTPUT_DIR / "catalogo-premium-2024.pdf", stream)


def build_price_list_pdf() -> None:
    lines = [
        LineSpec("F1", 24, "Lista mayorista · Vigencia septiembre 2024"),
        LineSpec("F2", 12, "Tarifas expresadas en pesos argentinos. IVA incluido.", leading=-28),
        LineSpec("F1", 16, "Lineas y presentaciones", leading=-34),
        LineSpec(
            "F2",
            12,
            "Vincent · VM Malbec joven · Caja 6 x 750 ml · Bot $8.900 · Caja $53.400",
            leading=-20,
        ),
        LineSpec(
            "F2",
            12,
            "Vincent · VCh Chardonnay · Caja 6 x 750 ml · Bot $9.200 · Caja $55.200",
            leading=-16,
        ),
        LineSpec(
            "F2",
            12,
            "Cuatro Fincas · Gran Malbec Reserva · Caja 6 x 750 ml · Bot $18.900 · Caja $107.400",
            leading=-18,
        ),
        LineSpec(
            "F2",
            12,
            "Cuatro Fincas · Cabernet Franc SV · Caja 6 x 750 ml · Bot $20.500 · Caja $116.900",
            leading=-16,
        ),
        LineSpec(
            "F2",
            12,
            "Raices Argentinas · Blend Andino · Caja 6 x 750 ml · Bot $14.500 · Caja $83.700",
            leading=-18,
        ),
        LineSpec(
            "F2",
            12,
            "Espumosos Metodo Tradicional · Extra Brut · Caja 6 x 750 ml · Bot $16.800 · Caja $95.800",
            leading=-16,
        ),
        LineSpec("F2", 12, "", leading=-18),
        LineSpec("F1", 16, "Escalas de descuento", leading=-34),
        LineSpec("F2", 12, "- 10% adicional por 5 cajas surtidas.", leading=-20),
        LineSpec("F2", 12, "- 15% adicional a partir de $120.000 por pedido.", leading=-16),
        LineSpec("F2", 12, "- Condiciones especiales para wine bars, hoteles y duty free.", leading=-16),
        LineSpec("F2", 12, "", leading=-18),
        LineSpec("F1", 16, "Logistica y cobranza", leading=-34),
        LineSpec(
            "F2",
            12,
            "- Envio refrigerado en Gran Mendoza sin cargo desde $45.000. Interior via Andreani Wine.",
            leading=-20,
        ),
        LineSpec(
            "F2",
            12,
            "- Plazos de pago: transferencia inmediata o 30 dias para clientes corporativos aprobados.",
            leading=-16,
        ),
        LineSpec(
            "F2",
            12,
            "- Seguimiento online de despacho y reporte de calidad post entrega.",
            leading=-16,
        ),
        LineSpec(
            "F3",
            11,
            "Consultas: mayoristas@cuatrofincas.com · Ejecutivo comercial +54 9 261 555 0000.",
            leading=-22,
        ),
    ]

    stream = _build_text_stream(lines)
    _write_pdf(OUTPUT_DIR / "lista-precios-mayoristas.pdf", stream)


def build_corporate_combos_pdf() -> None:
    lines = [
        LineSpec("F1", 24, "Degustaciones y combos corporativos 2024"),
        LineSpec("F2", 12, "Programas disenados por Salvador Patti para equipos y clientes.", leading=-28),
        LineSpec("F1", 16, "Combos seleccionados", leading=-34),
        LineSpec(
            "F2",
            12,
            "- Caja Regalo Premier · Gran Malbec Reserva + Cabernet Franc SV · Estuche madera · $36.200",
            leading=-20,
        ),
        LineSpec(
            "F2",
            12,
            "- Discovery Pack 6 botellas · Bonarda Organica, Chardonnay Reserva y Extra Brut · $93.900",
            leading=-16,
        ),
        LineSpec(
            "F2",
            12,
            "- Vertical Malbec 3 cosechas · Notas tecnicas impresas y ficha de cata · $58.500",
            leading=-16,
        ),
        LineSpec("F2", 12, "", leading=-18),
        LineSpec("F1", 16, "Experiencias guiadas", leading=-34),
        LineSpec(
            "F2",
            12,
            "- Degustacion en sala barrel room · 5 vinos premium + pairing finger food · $24.000 por persona.",
            leading=-20,
        ),
        LineSpec(
            "F2",
            12,
            "- Catas virtuales streaming · Kit enviado a domicilio con manual sensorial y soporte sommelier.",
            leading=-16,
        ),
        LineSpec(
            "F2",
            12,
            "- Team building vendimia · Recorrido por finca, cosecha simbolica y blend enologico.",
            leading=-16,
        ),
        LineSpec("F2", 12, "", leading=-18),
        LineSpec("F1", 16, "Servicios incluidos", leading=-34),
        LineSpec("F2", 12, "- Personalizacion con branding (grabado laser, tarjetas, sleeves).", leading=-20),
        LineSpec("F2", 12, "- Coordinacion logistica en Argentina y envios expresos a LATAM.", leading=-16),
        LineSpec("F2", 12, "- Reporte post experiencia con feedback de asistentes y metricas NPS.", leading=-16),
        LineSpec("F2", 12, "", leading=-18),
        LineSpec(
            "F3",
            11,
            "Reservas corporativas: eventos@cuatrofincas.com · +54 9 261 555 0015",
            leading=-22,
        ),
    ]

    stream = _build_text_stream(lines)
    _write_pdf(OUTPUT_DIR / "combos-degustacion-corporativas.pdf", stream)


def main() -> None:
    build_catalog_pdf()
    build_price_list_pdf()
    build_corporate_combos_pdf()
    print("Archivos actualizados en", OUTPUT_DIR)


if __name__ == "__main__":
    main()
