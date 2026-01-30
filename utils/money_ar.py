from decimal import Decimal
import re
from typing import Optional, Tuple

def parse_ars(value: str | float | int | None) -> Optional[Decimal]:
    """
    Parses a monetary value string (ARS convention) into a Decimal.
    Handles:
    - 43.200 (thousands dot)
    - 43200 (integer)
    - 43.200,50 (comma decimal)
    - $ 43.200
    - 10,41 (decimal)
    """
    if value is None:
        return None

    s = str(value).strip()
    if not s:
        return None

    # Remove symbol
    s = s.replace('$', '').replace('ARS', '').strip()

    # 1. Check for common ARS format: 1.234,56
    # If comma is present and is the last separator (or unique), treat as decimal
    # If dot is present, treat as thousands unless it's the only separator and clearly decimal (heuristic)

    # Simplest reliable heuristic for ARS:
    # - If ',' is present, replace '.' (thousands) with nothing, then ',' with '.'
    # - If only '.' is present:
    #    - If it has 3 digits after: ambiguous (could be thousands or decimal).
    #      Context suggests thousands in ARS for integers > 1000.
    #      But 10.50 is clearly decimal.
    # Let's use a robust approach:

    # Remove thousands separators if present
    # Case: 43.200 -> 43200 (dot is thousands)
    # Case: 43.200,50 -> 43200.50 (dot thousands, comma decimal)
    # Case: 10,41 -> 10.41 (comma decimal)
    # Case: 10.41 -> 10.41 (dot decimal, US style - risky but possible)

    if ',' in s:
        # Assume comma is decimal separator
        s = s.replace('.', '') # Remove thousands dot
        s = s.replace(',', '.') # Convert decimal comma to dot
    else:
        # No comma. Only dots?
        if '.' in s:
            parts = s.split('.')
            if len(parts) > 2:
                # 1.234.567 -> Thousands
                s = s.replace('.', '')
            elif len(parts) == 2:
                # 10.50 vs 1.200
                decimals = parts[1]
                if len(decimals) == 3:
                    # Likely thousands: 1.200 -> 1200
                    # BUT could be 1.234 (1 unit and 234 millis?). Unlikely in prices.
                    # Assume thousands
                    s = s.replace('.', '')
                else:
                    # Likely decimal: 10.50, 10.5
                    pass # Keep dot

    try:
        return Decimal(s)
    except:
        return None

def format_ars(value: Decimal | float | int, decimals: int = 2) -> str:
    """
    Formats a number as ARS currency string (e.g. 1.234,56).
    """
    if value is None:
        return ""
    try:
        # Standard formatting with thousands comma
        s = "{:,.{}f}".format(value, decimals)
        # Swap comma and dot to match ARS (1,234.56 -> 1.234,56)
        main, dec = s.split('.')
        main = main.replace(',', '.')
        return f"{main},{dec}"
    except:
        return str(value)
