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

from services.herramientas_municipio import direccion_es_valida


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
    """Delegates to direccion_es_valida from municipio tools."""
    return direccion_es_valida(direccion)