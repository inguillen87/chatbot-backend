from decimal import Decimal, ROUND_HALF_UP
import re


def parse_ars(value: str | None) -> Decimal:
    """
    Convierte strings tipo:
      "15.620,00" -> 15620.00
      "5.207"     -> 5207
      "$ 2.603,33"-> 2603.33
      "1 5.620,00"-> 15620.00 (pdfplumber a veces separa el primer dígito)
    """
    if value is None:
        return Decimal("0")

    text = str(value).strip().replace("$", "").strip()

    # Arregla casos tipo "1 5.620,00"
    text = re.sub(r"(\d)\s+(\d{1,3}[\.,]\d{3})", r"\1\2", text)

    # Saca espacios
    text = re.sub(r"\s+", "", text)

    if not text:
        return Decimal("0")

    # Si tiene coma, coma = decimales, punto = miles
    if "," in text:
        text = text.replace(".", "")
        text = text.replace(",", ".")
        return Decimal(text)

    # Si NO tiene coma pero tiene punto y son 3 dígitos al final -> punto = miles
    if "." in text and re.search(r"\.\d{3}$", text):
        return Decimal(text.replace(".", ""))

    return Decimal(text)


def format_ars(value: Decimal, decimals: int = 2) -> str:
    q = Decimal(10) ** -decimals
    v = value.quantize(q, rounding=ROUND_HALF_UP)

    s = f"{v:.{decimals}f}"
    if decimals == 0:
        int_part = s
        dec_part = ""
    else:
        int_part, dec_part = s.split(".")

    int_rev = int_part[::-1]
    grouped = ".".join([int_rev[i:i + 3] for i in range(0, len(int_rev), 3)])[::-1]

    if decimals == 0:
        return grouped
    return f"{grouped},{dec_part}"
