import re

EMAIL_RE = re.compile(r'[\w\.-]+@[\w\.-]+\.\w+', re.I)
PHONE_RE = re.compile(r'(?<!\d)(?:\+?\d{1,3})?\s*\d{6,12}(?!\d)')
DNI_RE = re.compile(r'(?<!\d)\d{7,9}(?!\d)')
SEP_RE = re.compile(r'[\,\|;]')


def parse_contact_line(text: str) -> dict:
    raw = text or ''
    parts = [p.strip() for p in SEP_RE.split(raw) if p.strip()]
    found: dict[str, str] = {}
    remaining: list[str] = []

    for part in parts:
        if EMAIL_RE.fullmatch(part):
            found['email'] = part
        elif DNI_RE.fullmatch(part):
            found['dni'] = part
        elif PHONE_RE.fullmatch(part.replace(' ', '')):
            found['telefono'] = re.sub(r'\s+', '', part)
        else:
            remaining.append(part)

    if remaining:
        found['nombre'] = remaining[0].title()
        if len(remaining) > 1:
            found['direccion_contacto'] = remaining[-1].lower()

    return found
