import re
from typing import Optional

try:
    from email_validator import validate_email, EmailNotValidError
except Exception:  # pragma: no cover - optional dependency
    validate_email = None
    EmailNotValidError = Exception

try:
    import phonenumbers
except Exception:  # pragma: no cover - optional dependency
    phonenumbers = None


def validate_name(nombre: str) -> bool:
    """Check that the name contains only letters and spaces."""
    if not nombre:
        return False
    return bool(re.fullmatch(r"[A-Za-zÁÉÍÓÚÑáéíóúñ ]{2,50}", nombre.strip()))


def validate_email_address(correo: str) -> bool:
    """Validate email format."""
    if not correo:
        return False
    correo = correo.strip()
    if validate_email:
        try:
            validate_email(correo, check_deliverability=False)
            return True
        except EmailNotValidError:
            return False
    return bool(re.fullmatch(r"[^@]+@[^@]+\.[^@]+", correo))


def normalize_phone(telefono: str, region: str = "AR") -> Optional[str]:
    """Return phone number in E.164 format or None if invalid."""
    if not telefono:
        return None
    telefono = telefono.strip()
    if phonenumbers:
        try:
            parsed = phonenumbers.parse(telefono, region)
            if not phonenumbers.is_valid_number(parsed):
                return None
            return phonenumbers.format_number(
                parsed, phonenumbers.PhoneNumberFormat.E164
            )
        except phonenumbers.NumberParseException:
            return None
    digits = re.sub(r"\D", "", telefono)
    if len(digits) < 8:
        return None
    if not telefono.startswith("+"):
        return "+" + digits
    return telefono


def validate_address(direccion: str) -> bool:
    """Delegates to ``direccion_es_valida`` from municipio tools."""
    from services.herramientas_municipio import direccion_es_valida
    return direccion_es_valida(direccion)


def extract_email(text: str) -> Optional[str]:
    """Extract the first valid email from a text string."""
    if not text:
        return None
    match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    if match and validate_email_address(match.group(0)):
        return match.group(0)
    return None


def extract_phone(text: str, region: str = "AR") -> Optional[str]:
    """Extract and normalize the first phone number found in text."""
    if not text:
        return None
    match = re.search(r"\+?\d[\d\s.-]{7,}\d", text)
    if match:
        return normalize_phone(match.group(0), region=region)
    return None


def extract_name(text: str) -> Optional[str]:
    """Extract a probable name from phrases like 'soy NAME' or 'me llamo NAME'."""
    if not text:
        return None
    m = re.search(r"(?:soy|me llamo|mi nombre es)\s+([A-Za-zÁÉÍÓÚÑáéíóúñ ]{2,50})", text, re.IGNORECASE)
    if m:
        candidate = m.group(1).strip()
        if validate_name(candidate):
            return candidate
    return None


def extract_address(text: str) -> Optional[str]:
    """Extract a simple address candidate from text using heuristics."""
    if not text:
        return None
    match = re.search(r"[A-Za-zÁÉÍÓÚÑáéíóúñ ]+\s+\d+[\w\s,]*", text)
    if match:
        addr = match.group(0).strip()
        if validate_address(addr):
            return addr
    return None


def extract_dni(text: str) -> Optional[str]:
    """Extract an Argentine DNI number (7-8 digits) from text."""
    if not text:
        return None
    match = re.search(r"\b\d{7,8}\b", text)
    if match:
        return match.group(0)
    return None


ADDRESS_RE = re.compile(
    r"\b([a-záéíóúñ]{2,}(?:\s+[a-záéíóúñ]{2,}){0,3}\s+\d{1,5}(?:\s+(?:esq\.?|esquina|y)\s+[a-záéíóúñ]{2,})?)\b",
    re.IGNORECASE,
)


def looks_like_address(text: str) -> bool:
    """Return True if text resembles a street address."""
    if not text:
        return False
    return bool(ADDRESS_RE.search(text))
