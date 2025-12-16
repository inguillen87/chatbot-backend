import os
import random
from datetime import datetime
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
import xlsxwriter

BASE_DIR = "static/demo"

# --- DATA DEFINITIONS ---

RUBRO_DATA = {
    "bodega": {
        "name": "Bodega Cuatro Fincas",
        "color": "#722F37",
        "pdfs": [
            {
                "filename": "catalogo-premium-2024.pdf",
                "title": "Portfolio Iconos 2024/2025",
                "content": [
                    ("Gran Malbec Reserva", "Crianza 18 meses. Notas de ciruela y tabaco. 96 pts.", "$18.900"),
                    ("Cabernet Franc SV", "Altamira, 1100 msnm. Estructura y elegancia.", "$22.500"),
                    ("Chardonnay Don Matías", "Fermentación en roble. Untuoso y fresco.", "$15.200"),
                    ("Bonarda Orgánica", "Certificación total. Fruta roja y especias.", "$13.200"),
                    ("Extra Brut", "24 meses sobre lías. Burbuja fina.", "$19.800")
                ],
                "type": "catalog"
            },
            {
                "filename": "combos-degustacion-corporativas.pdf",
                "title": "Experiencias Corporativas & Tasting",
                "content": [
                    ("Discovery Pack", "Cata virtual de 3 etiquetas con sommelier.", "Desde $45.000"),
                    ("Barrel Room Experience", "Visita privada, degustación de barricas y almuerzo.", "Consultar"),
                    ("Vendimia Empresarial", "Jornada de cosecha y asado para equipos.", "Febrero-Marzo")
                ],
                "type": "brochure"
            },
            # Also generate PDF for the price list referenced as PDF in some configs
            {
                "filename": "lista-precios-mayoristas.pdf",
                "title": "Lista de Precios Mayorista",
                "content": [
                    ("Gran Malbec (Caja x6)", "Precio unitario $18.900", "$113.400"),
                    ("Cabernet Franc (Caja x6)", "Precio unitario $22.500", "$135.000"),
                    ("Chardonnay (Caja x6)", "Precio unitario $15.200", "$91.200")
                ],
                "type": "list"
            }
        ],
        "excels": [
            {
                "filename": "lista-precios-mayoristas.xlsx",
                "headers": ["SKU", "Producto", "Variedad", "Caja x", "Precio Unitario", "Precio Caja", "Stock"],
                "data": [
                    ["BOD-001", "Gran Malbec", "Malbec", 6, 18900, 113400, "Alto"],
                    ["BOD-002", "Cabernet Franc SV", "Cabernet Franc", 6, 22500, 135000, "Medio"],
                    ["BOD-003", "Chardonnay Don Matías", "Chardonnay", 6, 15200, 91200, "Alto"],
                    ["BOD-004", "Bonarda Orgánica", "Bonarda", 6, 13200, 79200, "Bajo"],
                    ["BOD-005", "Extra Brut", "Espumante", 6, 19800, 118800, "Alto"]
                ]
            }
        ],
        "svgs": [
            {"filename": "dashboard-preview.svg", "type": "dashboard"},
            {"filename": "rutas-envio-premium.svg", "type": "map"},
            {"filename": "gran-malbec-reserva.svg", "type": "product_icon"}
        ]
    },
    "ferreteria": {
        "name": "Ferretería ProObra",
        "color": "#E67E22",
        "pdfs": [
            {
                "filename": "catalogo-materiales-obra.pdf",
                "title": "Catálogo Técnico & Estructural",
                "content": [
                    ("Hierro Conformado ADN-420", "Barras de 12m. Diámetros 6mm a 25mm.", "Consultar"),
                    ("Cemento Portland", "Bolsa 50kg. Alta resistencia inicial.", "$12.500"),
                    ("Ladrillo Hueco 18x18x33", "Pallet de 144 unidades. Cerámica roja.", "$450/u"),
                    ("Vigueta Pretensada", "Series T1 a T6. Longitudes hasta 7.20m.", "Variable"),
                    ("Malla Sima", "Q188 / Q335 / Q524. Paneles 2.40 x 6.00m.", "Consultar")
                ],
                "type": "catalog"
            },
            {
                "filename": "plan-logistica-obra.pdf",
                "title": "Planificación Logística de Obra",
                "content": [
                    ("Entrega en Obrador", "Coordinación con jefe de obra 24h antes.", "Incluido"),
                    ("Descarga con Pluma", "Servicio de hidrogrúa para pallets.", "Opcional"),
                    ("Acopio", "Reserva de materiales en depósito hasta 90 días.", "Bonificado")
                ],
                "type": "guide"
            }
        ],
        "excels": [
            {
                "filename": "stock-materiales.xlsx",
                "headers": ["Código", "Material", "Unidad", "Stock Actual", "Próximo Ingreso", "Precio Lista"],
                "data": [
                    ["CONST-001", "Cemento Portland 50kg", "Bolsa", 450, "18/12/2024", 12500],
                    ["CONST-002", "Cal Hidratada 25kg", "Bolsa", 200, "Disponible", 6800],
                    ["HIER-010", "Hierro 8mm", "Barra", 1200, "Disponible", 8500],
                    ["HIER-012", "Hierro 10mm", "Barra", 850, "20/12/2024", 14200],
                    ["LAD-18", "Ladrillo 18x18x33", "Pallet", 45, "Disponible", 64800]
                ]
            }
        ],
        "svgs": [
            {"filename": "dashboard-preview.svg", "type": "dashboard"},
            {"filename": "camion-grua.svg", "type": "vehicle_icon"}
        ]
    },
    "municipio": {
        "name": "Municipio Inteligente",
        "color": "#006C3F",
        "pdfs": [
            {
                "filename": "guia-tramites-rapidos.pdf",
                "title": "Guía de Trámites 2025",
                "content": [
                    ("Habilitación Comercial", "100% Digital. Requiere plano y contrato.", "72 hs"),
                    ("Licencia de Conducir", "Renovación express. Examen psicofísico in situ.", "En el día"),
                    ("Poda y Arbolado", "Solicitud de permiso o reclamo de intervención.", "15 días"),
                    ("Castración de Mascotas", "Turnos online. Zoonosis móvil.", "Gratuito"),
                    ("Libre Deuda", "Tasa de propiedad y comercio.", "Instantáneo")
                ],
                "type": "guide"
            },
            {
                "filename": "plan-iluminacion-inteligente.pdf",
                "title": "Plan de Obras LED",
                "content": [
                    ("Zona Norte", "Recambio de 450 luminarias. Avance 85%.", "Finaliza Ene '25"),
                    ("Barrio Centro", "Instalación de telegestión.", "Inicia Feb '25"),
                    ("Corredor Oeste", "Nuevas columnas y cableado subterráneo.", "En licitación")
                ],
                "type": "report"
            }
        ],
        "excels": [],
        "svgs": [
            {"filename": "dashboard-preview.svg", "type": "dashboard"},
            {"filename": "centro-monitoreo-smart.svg", "type": "network_icon"}
        ]
    },
    "logistica": {
        "name": "Axis Logística 3PL",
        "color": "#2980B9",
        "pdfs": [
            {
                "filename": "blueprint-operaciones-omnicanal.pdf",
                "title": "Blueprint Operativo",
                "content": [
                    ("Inbound", "Recepcion con RFID. Control de calidad muestral.", "SLA 4h"),
                    ("Almacenaje", "Racks selectivos y mezzanines para e-comm.", "WMS HighJump"),
                    ("Picking", "Wave picking y batching con put-to-light.", "99.8% Accuracy"),
                    ("Last Mile", "Ruteo dinámico y tracking en vivo.", "Same Day AMBA")
                ],
                "type": "technical"
            },
            {
                "filename": "matriz-sla-retail.pdf",
                "title": "SLA & KPIs - Q4 2024",
                "content": [
                    ("On Time In Full (OTIF)", "Objetivo 98%. Real 98.5%.", "Cumplido"),
                    ("Dock to Stock", "Objetivo < 6h. Real 4.2h.", "Cumplido"),
                    ("Inventory Accuracy", "Objetivo 99.9%. Real 99.95%.", "Cumplido")
                ],
                "type": "report"
            }
        ],
        "excels": [],
        "svgs": [
            {"filename": "dashboard-preview.svg", "type": "dashboard"},
            {"filename": "red-hubs.svg", "type": "network_map"}
        ]
    },
    "inmobiliaria": {
        "name": "Inmobiliaria Demo",
        "color": "#2C3E50",
        "pdfs": [
            {
                "filename": "portfolio-proyectos-prime.pdf",
                "title": "Desarrollos Exclusivos",
                "content": [
                    ("Torre Alvear", "Puerto Madero. Unidades de 2, 3 y 4 dorm.", "Desde USD 450k"),
                    ("Barrio Los Sauces", "Lotes al lago. Escritura inmediata.", "Desde USD 85k"),
                    ("Oficinas Libertador", "Plantas libres AAA. Certificación LEED.", "Alquiler USD 28/m2")
                ],
                "type": "catalog"
            },
            {
                "filename": "market-insights-q4.pdf",
                "title": "Reporte de Mercado Q4",
                "content": [
                    ("Residencial", "Recuperación de precios en corredor norte.", "+5% YoY"),
                    ("Oficinas", "Vacancia clase A en descenso.", "9.5% Vacancia"),
                    ("Logístico", "Demanda sostenida por e-commerce.", "Cap Rate 8%")
                ],
                "type": "report"
            },
            {
                "filename": "playbook-legal-onboarding.pdf",
                "title": "Playbook Legal & Onboarding",
                "content": [
                    ("Reserva Digital", "Proceso de seña con firma electrónica.", "Inmediato"),
                    ("Due Diligence", "Verificación de títulos y antecedentes.", "7 días"),
                    ("Escrituración", "Coordinación notarial.", "30 días")
                ],
                "type": "guide"
            }
        ],
        "excels": [],
        "svgs": [
            {"filename": "dashboard-preview.svg", "type": "dashboard"}
        ]
    },
    "almacen": {
        "name": "Almacén ByM",
        "color": "#E74C3C",
        "pdfs": [
            {
                "filename": "catalogo-combos-2024.pdf",
                "title": "Combos & Ofertas Semanales",
                "content": [
                    ("Pack Asado Familiar", "2kg Asado + 1kg Chorizo + Carbón + Vino.", "$35.000"),
                    ("Desayuno Premium", "Café molido + Tostadas + Mermelada + Jugo.", "$12.500"),
                    ("Limpieza Total", "Lavandina + Detergente + Desengrasante + Trapo.", "$8.900")
                ],
                "type": "catalog"
            },
            {
                "filename": "lista-precios-mayoristas.pdf",
                "title": "Lista Mayorista",
                "content": [
                    ("Gaseosa Cola 1.5L (x6)", "Precio bulto", "$10.800"),
                    ("Agua Mineral 2L (x6)", "Precio bulto", "$7.200"),
                    ("Arroz Largo Fino (x10)", "Precio bulto", "$18.500")
                ],
                "type": "list"
            }
        ],
        "excels": [
            {
                "filename": "lista-precios-mayoristas.xlsx",
                "headers": ["Rubro", "Producto", "Marca", "Pack x", "Precio Bulto"],
                "data": [
                    ["Bebidas", "Gaseosa Cola 1.5L", "Coca-Cola", 6, 10800],
                    ["Bebidas", "Agua Mineral 2L", "Villavicencio", 6, 7200],
                    ["Almacén", "Arroz Largo Fino", "Gallo", 10, 18500],
                    ["Almacén", "Fideos Tallarín", "Matarazzo", 20, 24000],
                    ["Limpieza", "Lavandina 1L", "Ayudín", 12, 15600]
                ]
            }
        ],
        "svgs": [
            {"filename": "dashboard-preview.svg", "type": "dashboard"},
            {"filename": "rutas-delivery.svg", "type": "map"}
        ]
    },
    "default": {
        "name": "Empresa Demo",
        "color": "#34495E",
        "pdfs": [
            {
                "filename": "catalogo-demo.pdf",
                "title": "Catálogo de Productos",
                "content": [
                    ("Producto A", "Descripción detallada del producto A.", "$100"),
                    ("Producto B", "Descripción detallada del producto B.", "$200"),
                    ("Servicio Premium", "Soporte 24/7 y consultoría.", "Consultar")
                ],
                "type": "catalog"
            },
            {
                "filename": "lista-precios.pdf",
                "title": "Lista de Precios Vigente",
                "content": [
                    ("Categoría 1", "Rango de precios estándar.", "$50 - $150"),
                    ("Categoría 2", "Rango de precios premium.", "$200 - $500")
                ],
                "type": "list"
            }
        ],
        "excels": [],
        "svgs": [
            {"filename": "dashboard-preview.svg", "type": "dashboard"}
        ]
    },
    "local_comercial": {
        "name": "Moda Urbana",
        "color": "#9B59B6",
        "pdfs": [
            {
                "filename": "lookbook-urbano.pdf",
                "title": "Lookbook AW 2024",
                "content": [
                    ("Abrigo Trench", "Lana merino. Corte clásico.", "$180.000"),
                    ("Jean Mom Fit", "Denim rígido. Lavado stone.", "$65.000"),
                    ("Camisa Poplin", "Algodón 100%. Oversize.", "$45.000")
                ],
                "type": "catalog"
            },
            {
                "filename": "programa-fidelizacion.pdf",
                "title": "Programa Loyalty",
                "content": [
                    ("Nivel Silver", "5% cashback. Cumpleaños.", "Gratis"),
                    ("Nivel Gold", "10% cashback. Envíos gratis.", "> $500k anual"),
                    ("Nivel Black", "15% cashback. Eventos VIP.", "> $1.5M anual")
                ],
                "type": "guide"
            }
        ],
        "excels": [],
        "svgs": [
            {"filename": "dashboard-preview.svg", "type": "dashboard"},
            {"filename": "combos-corporativos.svg", "type": "product_icon"}
        ]
    },
    "medico": {
        "name": "Clínica Demo",
        "color": "#27AE60",
        "pdfs": [
            {
                "filename": "guia-preparacion-estudios.pdf",
                "title": "Preparación para Estudios",
                "content": [
                    ("Análisis de Sangre", "Ayuno de 8 a 12 horas.", "7:00 - 10:00 hs"),
                    ("Ecografía Abdominal", "Ayuno 6hs + retención de líquidos.", "Con turno"),
                    ("Resonancia Magnética", "Sin objetos metálicos.", "Con turno")
                ],
                "type": "guide"
            },
            {
                "filename": "agenda-turnos-prioritarios.pdf",
                "title": "Agenda Prioritaria",
                "content": [
                    ("Pediatría", "Guardia activa 24h.", "Sin turno"),
                    ("Cardiología", "Turnos en el día para urgencias.", "Con derivación"),
                    ("Laboratorio", "Extracciones a domicilio.", "Con turno")
                ],
                "type": "guide"
            }
        ],
        "excels": [],
        "svgs": [
            {"filename": "dashboard-preview.svg", "type": "dashboard"},
            {"filename": "teleconsulta-flujo.svg", "type": "network_icon"}
        ]
    },
    "seguros": {
        "name": "ShieldOne Seguros",
        "color": "#3498DB",
        "pdfs": [
            {
                "filename": "coberturas-enterprise-2024.pdf",
                "title": "Pólizas Corporativas",
                "content": [
                    ("Flota Automotor", "Todo Riesgo con franquicia fija.", "Cobertura nacional"),
                    ("ART & Vida", "Ley de Riesgos + Seguro de Vida Obligatorio.", "A medida"),
                    ("Responsabilidad Civil", "Comprensiva y operaciones.", "Hasta USD 1M")
                ],
                "type": "catalog"
            },
            {
                "filename": "dashboard-siniestros-q3.pdf",
                "title": "Reporte de Siniestralidad",
                "content": [
                    ("Frecuencia", "Baja del 15% interanual.", "Positivo"),
                    ("Costo Medio", "Aumento por inflación repuestos.", "Alerta"),
                    ("Tiempo de Cierre", "Promedio 12 días.", "Objetivo 10 días")
                ],
                "type": "report"
            }
        ],
        "excels": [],
        "svgs": [
            {"filename": "dashboard-preview.svg", "type": "dashboard"},
            {"filename": "red-asistencia.svg", "type": "network_map"}
        ]
    },
    "fintech": {
        "name": "Fintech Demo",
        "color": "#8E44AD",
        "pdfs": [
            {
                "filename": "onboarding-digital-2024.pdf",
                "title": "Manual de Onboarding",
                "content": [
                    ("KYC", "Biometría facial + DNI.", "Real-time"),
                    ("Scoring", "Motor de decisión con bureau + open banking.", "< 30 seg"),
                    ("Alta de Cuenta", "CVU y tarjeta virtual inmediata.", "Instantáneo")
                ],
                "type": "guide"
            },
            {
                "filename": "suite-cobranzas-omnicanal.pdf",
                "title": "Suite Cobranzas",
                "content": [
                    ("Link de Pago", "Generación masiva vía API.", "Comisión variable"),
                    ("Debin", "Débito inmediato en cuenta.", "Bajo costo"),
                    ("QR Interoperable", "Cobros presenciales.", "Estándar BCRA")
                ],
                "type": "technical"
            }
        ],
        "excels": [],
        "svgs": [
            {"filename": "dashboard-preview.svg", "type": "dashboard"},
            {"filename": "mapa-apis.svg", "type": "network_icon"}
        ]
    },
    "energia": {
        "name": "Energía Demo",
        "color": "#F1C40F",
        "pdfs": [
            {
                "filename": "plan-upstream-2025.pdf",
                "title": "Plan de Inversión Upstream",
                "content": [
                    ("Pozo A-102", "Perforación dirigida.", "Q1 2025"),
                    ("Oleoducto Norte", "Mantenimiento mayor.", "Q2 2025")
                ],
                "type": "report"
            },
            {
                "filename": "tablero-operativo-downstream.pdf",
                "title": "KPIs Downstream",
                "content": [
                    ("Refinación", "92% capacidad instalada.", "Estable"),
                    ("Logística", "Despachos on-time 98%.", "Óptimo"),
                    ("Stock", "Crudo y derivados en tanques.", "Nivel seguridad")
                ],
                "type": "report"
            },
            {
                "filename": "matriz-hseq-licencias.pdf",
                "title": "Compliance HSEQ",
                "content": [
                    ("ISO 14001", "Recertificación ambiental.", "Aprobado"),
                    ("ISO 45001", "Seguridad y salud ocupacional.", "Aprobado"),
                    ("Permisos", "Renovación de licencias de extracción.", "En trámite")
                ],
                "type": "report"
            }
        ],
        "excels": [],
        "svgs": [
            {"filename": "dashboard-preview.svg", "type": "dashboard"}
        ]
    }
}

# --- GENERATOR FUNCTIONS ---

def create_pdf(filepath, title, content_list, rubro_name, color_hex, type="catalog"):
    doc = SimpleDocTemplate(filepath, pagesize=A4, rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=18)
    styles = getSampleStyleSheet()
    story = []

    header_style = ParagraphStyle(
        'HeaderStyle', parent=styles['Heading1'], textColor=colors.HexColor(color_hex),
        fontSize=24, spaceAfter=20, alignment=1
    )
    story.append(Paragraph(rubro_name, header_style))
    story.append(Paragraph(title, styles['Heading2']))
    story.append(Spacer(1, 12))

    date_style = ParagraphStyle('DateStyle', parent=styles['Normal'], alignment=2)
    story.append(Paragraph(f"Generado: {datetime.now().strftime('%d/%m/%Y')}", date_style))
    story.append(Spacer(1, 24))

    if type == "catalog":
        data = [["Producto / Servicio", "Descripción", "Precio / Ref"]]
    elif type == "report":
        data = [["Indicador / Ítem", "Detalle", "Estado / Valor"]]
    else:
        data = [["Concepto", "Detalle", "Observación"]]

    data.extend(content_list)

    t = Table(data, colWidths=[150, 250, 100])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor(color_hex)),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    story.append(t)

    story.append(Spacer(1, 48))
    footer_text = "Este documento es confidencial y para uso exclusivo de la demostración. © 2025 Chatboc Platform."
    story.append(Paragraph(footer_text, styles['Italic']))

    doc.build(story)
    print(f"Generated PDF: {filepath}")

def create_excel(filepath, headers, data, sheet_name="Lista"):
    workbook = xlsxwriter.Workbook(filepath)
    worksheet = workbook.add_worksheet(sheet_name)
    header_format = workbook.add_format({'bold': True, 'text_wrap': True, 'valign': 'top', 'fg_color': '#D7E4BC', 'border': 1})
    currency_format = workbook.add_format({'num_format': '$#,##0'})
    for col_num, value in enumerate(headers):
        worksheet.write(0, col_num, value, header_format)
    for row_num, row_data in enumerate(data, 1):
        for col_num, cell_value in enumerate(row_data):
            if isinstance(cell_value, int) or isinstance(cell_value, float):
                worksheet.write(row_num, col_num, cell_value, currency_format)
            else:
                worksheet.write(row_num, col_num, cell_value)
    worksheet.autofit()
    workbook.close()
    print(f"Generated Excel: {filepath}")

def create_svg(filepath, rubro_name, color_hex, type="dashboard"):
    width = 800
    height = 600
    content = ""

    if type == "dashboard":
        # Bar Chart
        bar_width = 50
        gap = 30
        start_x = 100
        base_y = 500
        values = [random.randint(100, 300) for _ in range(6)]
        labels = ["Ene", "Feb", "Mar", "Abr", "May", "Jun"]
        bars = ""
        for i, v in enumerate(values):
            x = start_x + i * (bar_width + gap)
            h = v
            y = base_y - h
            bars += f'<rect x="{x}" y="{y}" width="{bar_width}" height="{h}" fill="{color_hex}" opacity="0.8" />'
            bars += f'<text x="{x + bar_width/2}" y="{base_y + 20}" font-family="Arial" font-size="12" text-anchor="middle">{labels[i]}</text>'
            bars += f'<text x="{x + bar_width/2}" y="{y - 10}" font-family="Arial" font-size="12" text-anchor="middle" font-weight="bold">{v}</text>'
        content = f"""
          <text x="400" y="50" font-family="Arial" font-size="24" text-anchor="middle" fill="#333">Dashboard {rubro_name}</text>
          <line x1="80" y1="{base_y}" x2="750" y2="{base_y}" stroke="#333" stroke-width="2" />
          <line x1="80" y1="{base_y}" x2="80" y2="100" stroke="#333" stroke-width="2" />
          {bars}
        """
    elif type == "map" or type == "network_map":
        # Simplified Map/Network
        content = f"""
          <text x="400" y="50" font-family="Arial" font-size="24" text-anchor="middle" fill="#333">Mapa de Operaciones</text>
          <circle cx="200" cy="300" r="30" fill="{color_hex}" />
          <circle cx="400" cy="200" r="20" fill="{color_hex}" opacity="0.7" />
          <circle cx="600" cy="300" r="30" fill="{color_hex}" />
          <circle cx="400" cy="450" r="20" fill="{color_hex}" opacity="0.7" />
          <line x1="200" y1="300" x2="400" y2="200" stroke="#999" stroke-width="2" />
          <line x1="400" y1="200" x2="600" y2="300" stroke="#999" stroke-width="2" />
          <line x1="600" y1="300" x2="400" y2="450" stroke="#999" stroke-width="2" />
          <line x1="400" y1="450" x2="200" y2="300" stroke="#999" stroke-width="2" />
          <text x="200" y="350" text-anchor="middle">Hub Oeste</text>
          <text x="600" y="350" text-anchor="middle">Hub Este</text>
          <text x="400" y="180" text-anchor="middle">Central Norte</text>
          <text x="400" y="490" text-anchor="middle">Central Sur</text>
        """
    else:
        # Icon placeholder
        content = f"""
          <text x="400" y="50" font-family="Arial" font-size="24" text-anchor="middle" fill="#333">Recurso: {rubro_name}</text>
          <circle cx="400" cy="300" r="100" fill="{color_hex}" opacity="0.2" />
          <text x="400" y="300" font-family="Arial" font-size="60" text-anchor="middle" dominant-baseline="middle" fill="{color_hex}">ℹ️</text>
        """

    svg_content = f"""<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">
      <rect width="100%" height="100%" fill="#f9f9f9" />
      {content}
      <text x="400" y="580" font-family="Arial" font-size="12" text-anchor="middle" fill="#999">Generado por Chatboc Intelligence</text>
    </svg>"""

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(svg_content)
    print(f"Generated SVG: {filepath}")

def generate_all():
    print("🚀 Starting asset generation...")
    for rubro, data in RUBRO_DATA.items():
        folder_name = rubro
        if rubro == "medico_general": folder_name = "medico"
        output_dir = os.path.join(BASE_DIR, folder_name)
        os.makedirs(output_dir, exist_ok=True)
        print(f"📂 Processing {rubro} -> {output_dir}")
        for pdf_def in data.get("pdfs", []):
            path = os.path.join(output_dir, pdf_def["filename"])
            create_pdf(path, pdf_def["title"], pdf_def["content"], data["name"], data["color"], pdf_def["type"])
        for xls_def in data.get("excels", []):
            path = os.path.join(output_dir, xls_def["filename"])
            create_excel(path, xls_def["headers"], xls_def["data"])
        for svg_def in data.get("svgs", []):
            path = os.path.join(output_dir, svg_def["filename"])
            if not os.path.exists(path) or True: # Force overwrite to update style
                create_svg(path, data["name"], data["color"], svg_def["type"])
    print("✅ Assets generated successfully.")

if __name__ == "__main__":
    generate_all()
