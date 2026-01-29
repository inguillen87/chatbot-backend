from decimal import Decimal, ROUND_HALF_UP
import re

def parse_ars(s: str) -> Decimal:
    """
    Convierte strings tipo:
      "15.620,00" -> 15620.00
      "5.207"     -> 5207
      "$ 2.603,33"-> 2603.33
      "1 5.620,00"-> 15620.00 (pdfplumber a veces separa el primer dígito)
    """
    if s is None:
        return Decimal("0")

    s = str(s).strip()
    s = s.replace("$", "").strip()

    # arregla casos tipo "1 5.620,00"
    s = re.sub(r"(\d)\s+(\d{1,3}[\.,]\d{3})", r"\1\2", s)

    # saca espacios
    s = re.sub(r"\s+", "", s)

    # Si tiene coma, coma = decimales, punto = miles
    if "," in s:
        s = s.replace(".", "")
        s = s.replace(",", ".")
        return Decimal(s)

    # Si NO tiene coma pero tiene punto y son 3 dígitos al final -> punto = miles
    if "." in s and re.search(r"\.\d{3}$", s):
        return Decimal(s.replace(".", ""))

    # Caso simple
    return Decimal(s)

def format_ars(value: Decimal, decimals: int = 2) -> str:
    q = Decimal(10) ** -decimals
    v = value.quantize(q, rounding=ROUND_HALF_UP)

    s = f"{v:.{decimals}f}"     # "15620.00"
    if "." in s:
        int_part, dec_part = s.split(".")
    else:
        int_part = s
        dec_part = ""

    # miles con punto
    int_rev = int_part[::-1]
    grouped = ".".join([int_rev[i:i+3] for i in range(0, len(int_rev), 3)])[::-1]

    if decimals > 0:
        return f"{grouped},{dec_part}"
    return grouped
