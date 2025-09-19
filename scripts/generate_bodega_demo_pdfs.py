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
        LineSpec("F1", 26, "Cuatro Fincas Winery · Portfolio Comercial 2024/2025"),
        LineSpec(
            "F2",
            12,
            "Perdriel, Luján de Cuyo · Familia Patti · Enólogo jefe: Salvador Patti",
            leading=-30,
        ),
        LineSpec(
            "F3",
            11,
            "Viñedos en Altamira, Gualtallary, Agrelo y Vistalba · Agricultura sustentable",
            leading=-18,
        ),
        LineSpec("F1", 16, "Colección Íconos", leading=-34),
        LineSpec(
            "F2",
            12,
            "- Gran Malbec Reserva 2021 · Altamira + Agrelo · 14 meses roble francés de primer uso.",
            leading=-20,
        ),
        LineSpec(
            "F2",
            12,
            "  Perfil: mora negra, cassis y cacao amargo. Bot $18.900 · Caja x6 $107.400 (10% off).",
            leading=-16,
        ),
        LineSpec(
            "F2",
            12,
            "- Cabernet Franc Single Vineyard 2022 · Los Chacayes · barricas de 500 L 12 meses.",
            leading=-20,
        ),
        LineSpec(
            "F2",
            12,
            "  Perfil: hierbas andinas, grafito y cereza negra. Bot $20.500 · Caja x6 $116.900.",
            leading=-16,
        ),
        LineSpec(
            "F2",
            12,
            "- Chardonnay Finca Don Matías 2022 · Gualtallary · fermentación en barrica, batonage 8 meses.",
            leading=-20,
        ),
        LineSpec(
            "F2",
            12,
            "  Perfil: durazno blanco, miel de flores y avellanas. Bot $15.800 · Caja x6 $89.400.",
            leading=-16,
        ),
        LineSpec(
            "F2",
            12,
            "- Bonarda Orgánica Don Salvador 2023 · parrales de 45 años en Santa Rosa.",
            leading=-20,
        ),
        LineSpec(
            "F2",
            12,
            "  Perfil: fruta roja madura, anís y final especiado. Bot $13.200 · Caja x6 $74.800.",
            leading=-16,
        ),
        LineSpec(
            "F2",
            12,
            "- Extra Brut Pinot Noir · Chardonnay 2021 · Método tradicional, 18 meses sobre lías.",
            leading=-20,
        ),
        LineSpec(
            "F2",
            12,
            "  Perfil: brioche, frutos rojos frescos y acidez filosa. Bot $16.800 · Caja x6 $95.800.",
            leading=-16,
        ),
        LineSpec("F2", 12, "", leading=-18),
        LineSpec("F1", 16, "Colección HoReCa & Retail", leading=-34),
        LineSpec(
            "F2",
            12,
            "- Vincent Malbec Joven 2023 · 750 ml · Caja x6 · Bot $8.900 · Caja $53.400 · Min 4 cajas.",
            leading=-20,
        ),
        LineSpec(
            "F2",
            12,
            "- Vincent Chardonnay 2023 · 750 ml · Caja x6 · Bot $9.200 · Caja $55.200 · Min 4 cajas.",
            leading=-16,
        ),
        LineSpec(
            "F2",
            12,
            "- Blend Andino HoReCa · Corte Malbec, Syrah y Petit Verdot · Caja x6 $83.700.",
            leading=-16,
        ),
        LineSpec("F2", 12, "", leading=-18),
        LineSpec("F1", 16, "Experiencias & Hospitality", leading=-34),
        LineSpec(
            "F2",
            12,
            "- Degustaciones guiadas en sala de barricas · Viernes y sábados 18 h · Cupo 14 pax.",
            leading=-20,
        ),
        LineSpec(
            "F2",
            12,
            "  Incluye maridaje, cata vertical y obsequio souvenir. Tarifa $18.000 por persona.",
            leading=-16,
        ),
        LineSpec(
            "F2",
            12,
            "- Discovery Pack 6 botellas · Selección curada para activaciones virtuales y kits streaming.",
            leading=-20,
        ),
        LineSpec(
            "F2",
            12,
            "- Team building Vendimia · cosecha simbólica, blend personalizado y certificado digital.",
            leading=-16,
        ),
        LineSpec("F2", 12, "", leading=-18),
        LineSpec("F1", 16, "Servicios para trade", leading=-34),
        LineSpec(
            "F2",
            12,
            "- Etiquetas personalizadas, sleeves y estuches corporativos con grabado láser.",
            leading=-20,
        ),
        LineSpec(
            "F2",
            12,
            "- Reportes de rotación, NPS post experiencia y soporte de sommelier en activaciones.",
            leading=-16,
        ),
        LineSpec("F2", 12, "", leading=-18),
        LineSpec("F1", 16, "Contacto & Logística", leading=-34),
        LineSpec(
            "F2",
            12,
            "- Envío bonificado en Gran Mendoza desde $45.000 · Interior 48/72 h con Andreani Wine.",
            leading=-20,
        ),
        LineSpec(
            "F2",
            12,
            "- Soporte comercial: ventas@cuatrofincas.com · +54 9 261 555 0000.",
            leading=-16,
        ),
        LineSpec(
            "F3",
            11,
            "Servicio recomendado: tintos 16 °C · blancos/espumosos 10 °C · decantar 45 minutos.",
            leading=-22,
        ),
    ]

    stream = _build_text_stream(lines)
    _write_pdf(OUTPUT_DIR / "catalogo-premium-2024.pdf", stream)


def build_price_list_pdf() -> None:
    lines = [
        LineSpec("F1", 24, "Lista mayorista · Vigencia noviembre 2024"),
        LineSpec("F2", 12, "Precios en pesos argentinos, IVA incluido. Valores EXW bodega.", leading=-28),
        LineSpec("F1", 16, "Líneas disponibles", leading=-34),
        LineSpec(
            "F2",
            12,
            "- Vincent Malbec joven 750 ml · Caja x6 · Bot $8.900 · Caja $53.400 · Min 4 cajas.",
            leading=-20,
        ),
        LineSpec(
            "F2",
            12,
            "- Vincent Chardonnay joven 750 ml · Caja x6 · Bot $9.200 · Caja $55.200 · Min 4 cajas.",
            leading=-16,
        ),
        LineSpec(
            "F2",
            12,
            "- Gran Malbec Reserva 2021 · Caja x6 · Bot $18.900 · Caja $107.400 · Min 2 cajas.",
            leading=-16,
        ),
        LineSpec(
            "F2",
            12,
            "- Cabernet Franc Single Vineyard 2022 · Caja x6 · Bot $20.500 · Caja $116.900 · Min 2 cajas.",
            leading=-16,
        ),
        LineSpec(
            "F2",
            12,
            "- Blend Andino HoReCa · Caja x6 · Bot $14.500 · Caja $83.700 · Min 3 cajas.",
            leading=-16,
        ),
        LineSpec(
            "F2",
            12,
            "- Extra Brut Método Tradicional 2021 · Caja x6 · Bot $16.800 · Caja $95.800 · Min 3 cajas.",
            leading=-16,
        ),
        LineSpec("F2", 12, "", leading=-18),
        LineSpec("F1", 16, "Bonificaciones y programas", leading=-34),
        LineSpec("F2", 12, "- 10% adicional en pedidos combinados de 5 cajas o más.", leading=-20),
        LineSpec("F2", 12, "- 15% adicional a partir de $120.000 por orden o 30 cajas trimestrales.", leading=-16),
        LineSpec("F2", 12, "- Programa HoReCa: copas Riedel + training staff en pedidos desde 12 cajas.", leading=-16),
        LineSpec("F2", 12, "- Duty Free & cruceros: condiciones FOB a medida + soporte documental.", leading=-16),
        LineSpec("F2", 12, "", leading=-18),
        LineSpec("F1", 16, "Logística y cobranza", leading=-34),
        LineSpec(
            "F2",
            12,
            "- Envío refrigerado Gran Mendoza sin cargo desde $45.000 · interior 48/72 h Andreani Wine.",
            leading=-20,
        ),
        LineSpec(
            "F2",
            12,
            "- Capital Federal: salidas semanales con Cargo Services Aéreo · delivery premium viernes.",
            leading=-16,
        ),
        LineSpec(
            "F2",
            12,
            "- Plazos: transferencia inmediata o 30/60 días con dossier comercial aprobado.",
            leading=-16,
        ),
        LineSpec(
            "F2",
            12,
            "- Seguimiento online + reporte de temperatura y satisfacción 48 h post entrega.",
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
        LineSpec("F1", 24, "Degustaciones & combos corporativos 2024"),
        LineSpec(
            "F2",
            12,
            "Programas diseñados por Salvador Patti para fidelizar clientes y equipos.",
            leading=-28,
        ),
        LineSpec("F1", 16, "Combos destacados", leading=-34),
        LineSpec(
            "F2",
            12,
            "- Caja Regalo Premier · Gran Malbec Reserva + Cabernet Franc SV · Estuche de madera grabado · $36.200.",
            leading=-20,
        ),
        LineSpec(
            "F2",
            12,
            "- Discovery Pack 6 botellas · Bonarda Orgánica, Chardonnay Reserva y Extra Brut · $93.900.",
            leading=-16,
        ),
        LineSpec(
            "F2",
            12,
            "- Vertical Malbec (2019/2020/2021) · notas técnicas impresas + QR con cata guiada · $58.500.",
            leading=-16,
        ),
        LineSpec(
            "F2",
            12,
            "- Pack HoReCa Express · 12 botellas mixtas + copas Riedel + capacitación staff · $142.000.",
            leading=-16,
        ),
        LineSpec("F2", 12, "", leading=-18),
        LineSpec("F1", 16, "Experiencias guiadas", leading=-34),
        LineSpec(
            "F2",
            12,
            "- Barrel Room Experience · 5 vinos íconos + finger food regional · $24.000 por persona (mín 10 pax).",
            leading=-20,
        ),
        LineSpec(
            "F2",
            12,
            "- Streaming Premium · kit enviado a domicilio + maridaje sugerido + sommelier en vivo.",
            leading=-16,
        ),
        LineSpec(
            "F2",
            12,
            "- Vendimia Corporativa · visita a finca, cosecha simbólica y blend personalizado con etiqueta.",
            leading=-16,
        ),
        LineSpec("F2", 12, "", leading=-18),
        LineSpec("F1", 16, "Servicios incluidos", leading=-34),
        LineSpec("F2", 12, "- Branding completo: sleeves, tarjetas dedicadas y notas manuscritas.", leading=-20),
        LineSpec("F2", 12, "- Coordinación logística nacional + envíos express LATAM vía partners especializados.", leading=-16),
        LineSpec("F2", 12, "- Reporte de asistencia, métricas NPS y biblioteca multimedia post evento.", leading=-16),
        LineSpec("F2", 12, "- Equipo de hospitality bilingüe + opción de sommeliers invitados.", leading=-16),
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
