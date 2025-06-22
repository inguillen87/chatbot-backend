try:
    from fpdf import FPDF
except Exception:  # pragma: no cover - optional dependency
    FPDF = None
from typing import List, Dict, Any


def generar_presupuesto_pdf(items: List[Dict[str, Any]], cliente: Dict[str, Any]) -> bytes:
    """Genera un PDF simple de presupuesto y devuelve su contenido en bytes."""
    if FPDF is None:
        raise RuntimeError("fpdf2 library is required to generar_presupuesto_pdf")
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=12)

    pdf.cell(0, 10, "Presupuesto", ln=True)
    nombre = cliente.get("nombre")
    if nombre:
        pdf.cell(0, 10, f"Cliente: {nombre}", ln=True)
    pdf.ln(4)

    pdf.cell(70, 8, "Producto", 1)
    pdf.cell(30, 8, "Cant.", 1)
    pdf.cell(30, 8, "Precio", 1)
    pdf.cell(40, 8, "Subtotal", 1, ln=True)
    total = 0.0
    for it in items:
        nombre_it = str(it.get("nombre", ""))[:40]
        cantidad = float(it.get("cantidad", 1) or 1)
        precio = float(it.get("precio", 0) or 0)
        subtotal = cantidad * precio
        pdf.cell(70, 8, nombre_it, 1)
        pdf.cell(30, 8, f"{cantidad:.0f}", 1)
        pdf.cell(30, 8, f"${precio:.2f}", 1)
        pdf.cell(40, 8, f"${subtotal:.2f}", 1, ln=True)
        total += subtotal

    pdf.ln(4)
    pdf.cell(0, 10, f"Total: ${total:.2f}", ln=True)
    return pdf.output(dest="S").encode("latin1")
