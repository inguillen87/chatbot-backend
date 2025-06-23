try:
    from fpdf import FPDF
except Exception:  # pragma: no cover - optional dependency
    FPDF = None
from typing import List, Dict, Any
from datetime import datetime


def generar_presupuesto_pdf(items: List[Dict[str, Any]], cliente: Dict[str, Any]) -> bytes:
    """Genera un PDF simple de presupuesto y devuelve su contenido en bytes."""
    if FPDF is None:
        raise RuntimeError("fpdf2 library is required to generar_presupuesto_pdf")
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=12)

    pdf.cell(0, 10, "Presupuesto", ln=True)
    nombre = cliente.get("nombre", "N/D")
    pdf.cell(0, 10, f"Cliente: {nombre}", ln=True)
    if cliente.get("cuit"):
        pdf.cell(0, 10, f"CUIT: {cliente['cuit']}", ln=True)
    if cliente.get("direccion"):
        pdf.cell(0, 10, f"Dirección: {cliente['direccion']}", ln=True)
    pdf.cell(0, 10, f"Fecha: {datetime.now().strftime('%d/%m/%Y')}", ln=True)
    pdf.ln(4)

    pdf.set_fill_color(230, 230, 230)
    pdf.cell(70, 8, "Producto", 1, 0, 'C', True)
    pdf.cell(30, 8, "Cant.", 1, 0, 'C', True)
    pdf.cell(30, 8, "Precio", 1, 0, 'C', True)
    pdf.cell(40, 8, "Subtotal", 1, 1, 'C', True)
    total = 0.0
    for idx, it in enumerate(items):
        if pdf.get_y() > 260:  # Salto de página si es necesario
            pdf.add_page()
            pdf.set_font("Arial", size=12)
        nombre_it = str(it.get("nombre", "N/D"))[:40]
        cantidad = float(it.get("cantidad", 1) or 1)
        precio = float(it.get("precio", 0) or 0)
        subtotal = cantidad * precio
        pdf.cell(70, 8, nombre_it, 1)
        pdf.cell(30, 8, f"{cantidad:.0f}", 1, 0, 'R')
        pdf.cell(30, 8, f"${precio:,.2f}", 1, 0, 'R')
        pdf.cell(40, 8, f"${subtotal:,.2f}", 1, 1, 'R')
        total += subtotal

    pdf.ln(4)
    pdf.set_font("Arial", "B", 12)
    pdf.cell(0, 10, f"Total: ${total:,.2f}", ln=True)
    return pdf.output(dest="S").encode("latin1")
