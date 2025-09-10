import re

EMAIL_RE = re.compile(r'[\w\.-]+@[\w\.-]+\.\w+', re.I)
PHONE_RE = re.compile(r'(?<!\d)(?:\+?\d{1,3})?\s*\d{6,12}(?!\d)')
DNI_RE = re.compile(r'(?<!\d)\d{7,9}(?!\d)')
SEP_RE = re.compile(r'[\,\|;]')


def parse_contact_line(text: str) -> dict:
    """Extract contact fields from a compact user text.

    The parser is tolerant to separators: commas, pipes, semicolons or just spaces.
    The fields can appear in any order. Anything left after removing recognised
    patterns is considered part of the name, with the last token optionally
    treated as a contact address if a separator is present.
    """

    raw = text or ''
    found: dict[str, str] = {}
    working = raw

    # Extract known patterns first and remove them from the working string
    email_m = EMAIL_RE.search(working)
    if email_m:
        found['email'] = email_m.group(0)
        working = working.replace(email_m.group(0), ' ')

    dni_m = DNI_RE.search(working)
    if dni_m:
        found['dni'] = dni_m.group(0)
        working = working.replace(dni_m.group(0), ' ')

    phone_m = PHONE_RE.search(working)
    if phone_m:
        phone_raw = phone_m.group(0)
        found['telefono'] = re.sub(r'\s+', '', phone_raw)
        working = working.replace(phone_raw, ' ')

    # Remaining text may still have separators; try to isolate name and address
    tokens = [p.strip() for p in SEP_RE.split(working) if p.strip()]
    nombre_tokens: list[str]
    if tokens:
        if len(tokens) > 1:
            found['direccion_contacto'] = tokens[-1].lower()
            nombre_tokens = tokens[:-1]
        else:
            nombre_tokens = tokens
    else:
        nombre_tokens = [t for t in working.split() if t]

    if nombre_tokens:
        found['nombre'] = ' '.join(nombre_tokens).title()

    return found
