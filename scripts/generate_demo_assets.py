from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Sequence


@dataclass
class Section:
    title: str
    bullets: Sequence[str]


@dataclass
class TableSpec:
    headers: Sequence[str]
    rows: Sequence[Sequence[str]]


@dataclass
class PdfSpec:
    output: Path
    title: str
    subtitle: str
    highlights: Sequence[str] = field(default_factory=tuple)
    sections: Sequence[Section] = field(default_factory=tuple)
    table: TableSpec | None = None
    footer: str = ""


def _pdf_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
    )


def _text_cmd(font: str, size: float, x: float, y: float, text: str) -> str:
    escaped = _pdf_escape(text)
    return f"BT /{font} {size:.0f} Tf {x:.2f} {y:.2f} Td ({escaped}) Tj ET\n"


def _line_cmd(x1: float, y1: float, x2: float, y2: float) -> str:
    return f"{x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S\n"


def _build_pdf_stream(spec: PdfSpec) -> str:
    y = 738.0
    commands: List[str] = []

    def drop(amount: float) -> None:
        nonlocal y
        y -= amount

    commands.append(_text_cmd("F2", 24, 54, y, spec.title.upper()))
    drop(30)

    if spec.subtitle:
        commands.append(_text_cmd("F3", 14, 54, y, spec.subtitle))
        drop(26)

    if spec.highlights:
        commands.append(_text_cmd("F2", 14, 54, y, "HIGHLIGHTS"))
        drop(18)
        for bullet in spec.highlights:
            commands.append(_text_cmd("F1", 11, 54, y, f"· {bullet}"))
            drop(14)

    for section in spec.sections:
        commands.append(_text_cmd("F2", 14, 54, y, section.title.upper()))
        drop(18)
        for bullet in section.bullets:
            commands.append(_text_cmd("F1", 11, 54, y, f"· {bullet}"))
            drop(14)

    if spec.table and spec.table.headers and spec.table.rows:
        drop(6)
        table_top = y
        col_positions = [54.0, 210.0, 346.0, 462.0, 558.0]
        row_height = 16.0

        # Header row
        header_y = table_top - 14
        for idx, header in enumerate(spec.table.headers):
            x = col_positions[idx] if idx < len(col_positions) - 1 else col_positions[-2]
            commands.append(_text_cmd("F2", 11, x, header_y, header))

        # Rows
        body_start_y = table_top - row_height - 6
        for row_idx, row in enumerate(spec.table.rows):
            text_y = body_start_y - row_idx * row_height
            for col_idx, value in enumerate(row):
                if col_idx >= len(col_positions) - 1:
                    x_val = col_positions[-2]
                else:
                    x_val = col_positions[col_idx]
                commands.append(_text_cmd("F1", 10, x_val, text_y, value))
        num_rows = len(spec.table.rows)
        table_bottom = table_top - (num_rows + 1) * row_height - 2

        # Table grid
        commands.append(_line_cmd(col_positions[0], table_top, col_positions[-1], table_top))
        commands.append(_line_cmd(col_positions[0], table_bottom, col_positions[-1], table_bottom))
        for x in col_positions:
            commands.append(_line_cmd(x, table_top, x, table_bottom))

        y = table_bottom - 28

    if spec.footer:
        footer_y = 44.0
        commands.append(_text_cmd("F3", 9, 54, footer_y, spec.footer))

    return "".join(commands)


def _write_pdf(spec: PdfSpec) -> None:
    stream_text = _build_pdf_stream(spec)
    stream_bytes = stream_text.encode("latin-1")

    objects: List[bytes] = []
    objects.append(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
    objects.append(b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n")
    objects.append(
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R /F2 6 0 R /F3 7 0 R >> >> >>\nendobj\n"
    )
    objects.append(
        b"4 0 obj\n<< /Length "
        + str(len(stream_bytes)).encode("ascii")
        + b" >>\nstream\n"
        + stream_bytes
        + b"endstream\nendobj\n"
    )
    objects.append(b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n")
    objects.append(b"6 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>\nendobj\n")
    objects.append(b"7 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Oblique >>\nendobj\n")

    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0] * (len(objects) + 1)
    for idx, obj in enumerate(objects, start=1):
        offsets[idx] = len(pdf)
        pdf.extend(obj)

    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for idx in range(1, len(objects) + 1):
        pdf.extend(f"{offsets[idx]:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode("ascii")
    )

    spec.output.parent.mkdir(parents=True, exist_ok=True)
    spec.output.write_bytes(pdf)


def build_pdf(spec: PdfSpec) -> None:
    _write_pdf(spec)
    print(f"✓ Generado {spec.output}")


def main() -> None:
    base = Path(__file__).resolve().parent.parent / "static" / "demo"

    specs: List[PdfSpec] = [
        PdfSpec(
            output=base / "fintech" / "onboarding-digital-2024.pdf",
            title="Playbook Onboarding Digital 2024",
            subtitle="Kits KYC, scoring biométrico y journeys omnicanal",
            highlights=[
                "Aprobación promedio 3 min con biometría y listas negras",
                "Segmentación de flujos retail, premium y business",
                "Integración directa con core bancario y CRM",
            ],
            sections=[
                Section(
                    title="Métricas clave",
                    bullets=[
                        "Tasa de completitud 92% en onboarding mobile",
                        "Reducción de fraude inicial 38% con IA antifishing",
                        "Segmentación dinámica por score y comportamiento",
                    ],
                ),
                Section(
                    title="Roadmap implementación",
                    bullets=[
                        "Semana 0-2: discovery y diseño UX",
                        "Semana 3-6: integraciones y pruebas reguladas",
                        "Semana 7: go-live + monitoreo hiper-care",
                    ],
                ),
            ],
            table=TableSpec(
                headers=["Segmento", "Producto", "Tiempo de alta", "Conversión"],
                rows=[
                    ["Retail fintech", "Cuenta digital + tarjeta", "5 minutos", "78%"],
                    ["Pyme", "Cuenta + gateway pagos", "12 minutos", "65%"],
                    ["Premium", "Cuenta multicurrency", "20 minutos", "54%"],
                ],
            ),
            footer="Confidencial · Aurora Fintech Hub · Actualización octubre 2024",
        ),
        PdfSpec(
            output=base / "fintech" / "suite-cobranzas-omnicanal.pdf",
            title="Suite de Cobranzas Omnicanal",
            subtitle="Débitos, billeteras, links dinámicos y conciliación automática",
            highlights=[
                "Recupero +30% con campañas proactivas",
                "Plugins listos para Magento, VTEX y WooCommerce",
                "Conciliación automática multi PSP",
            ],
            sections=[
                Section(
                    title="Componentes",
                    bullets=[
                        "Links dinámicos con vencimiento y recordatorios",
                        "Botón de pago embebible en portal y app",
                        "Módulo de cobranza preventiva con IA conversacional",
                    ],
                ),
                Section(
                    title="Alertas operativas",
                    bullets=[
                        "Monitoreo de caídas PSP y fallback automático",
                        "Panel de morosidad por cohorte y región",
                        "Alertas sobre conciliaciones pendientes",
                    ],
                ),
            ],
            table=TableSpec(
                headers=["Canal", "Uso", "SLA liquidación", "% adopción"],
                rows=[
                    ["Débito automático", "Suscripciones", "24 h", "48%"],
                    ["Wallets", "Retail & loyalty", "Instantáneo", "31%"],
                    ["Transferencias API", "B2B high ticket", "2 h", "21%"],
                ],
            ),
            footer="Aurora Fintech Hub · Suite de cobranzas · Vigencia Q4 2024",
        ),
        PdfSpec(
            output=base / "seguros" / "coberturas-enterprise-2024.pdf",
            title="Catálogo Coberturas Enterprise 2024",
            subtitle="Planes combinados para corporaciones regionales",
            highlights=[
                "Planes multinacionales con asistencia 24/7",
                "Gestión integral de beneficios ejecutivos",
                "Integración con ERPs y portales HR",
            ],
            sections=[
                Section(
                    title="Programas destacados",
                    bullets=[
                        "Flotas inteligentes con telemetría y coaching",
                        "Property + Business Interruption con sumas aseguradas altas",
                        "Cobertura Cyber con respuesta de incidentes",
                    ],
                ),
                Section(
                    title="Beneficios",
                    bullets=[
                        "Mesa ejecutiva 24/7 para cuentas estratégicas",
                        "Asistencia internacional y beneficios premium",
                        "Cláusulas tailor-made con retenciones flexibles",
                    ],
                ),
            ],
            table=TableSpec(
                headers=["Programa", "Cobertura", "Suma asegurada", "Deducible"],
                rows=[
                    ["Flotas", "Casco + RC + asistencia", "USD 20M", "USD 1.5K"],
                    ["Property", "Incendio + BI + contingencias", "USD 85M", "USD 25K"],
                    ["Cyber", "Respuesta incidentes + PR", "USD 10M", "USD 5K"],
                ],
            ),
            footer="ShieldOne Seguros Corporativos · Confidential · Octubre 2024",
        ),
        PdfSpec(
            output=base / "seguros" / "dashboard-siniestros-q3.pdf",
            title="Dashboard Ejecutivo de Siniestros Q3",
            subtitle="Frecuencia, severidad y tiempos de indemnización",
            highlights=[
                "Datos consolidados LATAM + USA",
                "Actualización en vivo desde data lake",
                "Alertas tempranas y benchmarks",
            ],
            sections=[
                Section(
                    title="Insight rápido",
                    bullets=[
                        "Severidad promedio USD 4.2K (-8% QoQ)",
                        "Frecuencia 2.3% con foco en retail",
                        "Tiempo medio de indemnización 6.2 días",
                    ],
                ),
                Section(
                    title="Acciones",
                    bullets=[
                        "Campaña de prevención en flotas high risk",
                        "Negociación con talleres para reducir tiempos",
                        "Ajustes en retenciones property high severity",
                    ],
                ),
            ],
            table=TableSpec(
                headers=["Línea", "Frecuencia", "Severidad", "Tiempo indemnización"],
                rows=[
                    ["Flotas", "3.1%", "USD 3.8K", "5.4 días"],
                    ["Property", "1.2%", "USD 6.7K", "8.1 días"],
                    ["Salud corporate", "2.8%", "USD 2.1K", "4.6 días"],
                ],
            ),
            footer="ShieldOne · Reporting ejecutivo · Septiembre 2024",
        ),
        PdfSpec(
            output=base / "logistica" / "blueprint-operaciones-omnicanal.pdf",
            title="Blueprint Operaciones Omnicanal",
            subtitle="Diseño de nodos, slots y flujos WMS/TMS integrados",
            highlights=[
                "Lead time <24 h en AMBA",
                "Sincronización OMS + marketplaces",
                "Torre de control con visibilidad 360°",
            ],
            sections=[
                Section(
                    title="Layout propuesto",
                    bullets=[
                        "Hubs regionales + micro fulfillment urbano",
                        "Slots dinámicos para inbound/outbound",
                        "Integración WMS/TMS + torre comando",
                    ],
                ),
                Section(
                    title="KPIs clave",
                    bullets=[
                        "Fill rate 97% en campañas peak",
                        "OTIF 95% same-day AMBA",
                        "Reducción 22% costo última milla",
                    ],
                ),
            ],
            table=TableSpec(
                headers=["Nodo", "Capacidad", "Servicio", "SLA"],
                rows=[
                    ["CABA", "22K pedidos/día", "Same-day", "<12 h"],
                    ["Córdoba", "12K pedidos/día", "Next-day", "24 h"],
                    ["Mendoza", "6K pedidos/día", "Crossdock regional", "36 h"],
                ],
            ),
            footer="Axis Logistics 3PL · Blueprint omnicanal · Octubre 2024",
        ),
        PdfSpec(
            output=base / "logistica" / "matriz-sla-retail.pdf",
            title="Matriz SLA Retail & Ecommerce",
            subtitle="KPIs por vertical, carrier y ventana de entrega",
            highlights=[
                "Monitoreo 24/7 desde torre de control",
                "Alertas proactivas por desviación",
                "Benchmark regional actualizable",
            ],
            sections=[
                Section(
                    title="Coberturas",
                    bullets=[
                        "Same-day urbano con flota eléctrica",
                        "Next-day nacional con carriers aliados",
                        "Crossborder express Chile/Uruguay",
                    ],
                ),
                Section(
                    title="Uso recomendado",
                    bullets=[
                        "Moda y beauty con pedidos de alto valor",
                        "Electrónica con embalaje reforzado",
                        "Retail masivo en campañas hot sale",
                    ],
                ),
            ],
            table=TableSpec(
                headers=["Vertical", "Carrier", "Ventana", "Nivel SLA"],
                rows=[
                    ["Moda", "Axis Express", "Same-day", "96%"],
                    ["Beauty", "Carrier aliado", "Next-day", "94%"],
                    ["Tech", "Aéreo + última milla", "48 h", "92%"],
                ],
            ),
            footer="Axis Logistics 3PL · Matriz SLA · Octubre 2024",
        ),
    ]

    for spec in specs:
        build_pdf(spec)


if __name__ == "__main__":
    main()
