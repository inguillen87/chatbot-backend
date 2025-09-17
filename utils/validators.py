import re
from typing import Optional

from services.vocabulary_loader import get_name_prefix_stopwords

NAME_PREFIX_STOPWORDS = get_name_prefix_stopwords()

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
    """
    Return phone number in E.164 format or None if invalid.
    Includes specific logic for Argentinian mobile numbers.
    """
    if not isinstance(telefono, str):
        return None

    solo_numeros = re.sub(r"\D", "", telefono)
    if len(solo_numeros) < 8:
        return None

    # Use phonenumbers library if available for robust parsing
    if phonenumbers:
        try:
            # The library is smart enough to handle most cases if region is correct
            parsed = phonenumbers.parse(solo_numeros, region)
            if phonenumbers.is_valid_number(parsed):
                return phonenumbers.format_number(
                    parsed, phonenumbers.PhoneNumberFormat.E164
                )
        except phonenumbers.NumberParseException:
            # If library fails, fallback to manual formatting
            pass

    # Manual fallback formatting (less robust, but covers common AR cases)
    cod_pais = "54"
    if solo_numeros.startswith(cod_pais):
        if len(solo_numeros) == 12 and not solo_numeros.startswith('549'):
            return f"+549{solo_numeros[2:]}"
        return f"+{solo_numeros}"

    if region == "AR":
        if solo_numeros.startswith('0'):
            solo_numeros = solo_numeros[1:]
        if solo_numeros.startswith('15'):
            solo_numeros = solo_numeros[2:]
        if len(solo_numeros) == 10:
            return f"+{cod_pais}9{solo_numeros}"
        return f"+{cod_pais}{solo_numeros}"

    return f"+{cod_pais}{solo_numeros}"


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


def extract_phone(text: str, region: str = "AR") -> Optional[tuple[str, str]]:
    """
    Extract and normalize the first phone number found in text.
    Returns a tuple of (normalized_phone, raw_match) or None.
    """
    if not text:
        return None
    # This regex is more stable for finding a single phone number without being too greedy.
    match = re.search(r"(\+?\d[\d\s.-]{7,18}\d)", text)
    if match:
        raw_match = match.group(0).strip()
        normalized = normalize_phone(raw_match, region=region)
        if normalized:
            return normalized, raw_match
    return None


def _sanitize_name_candidate(value: str) -> str:
    """Normalize spacing and drop non-letter characters for name detection."""

    cleaned = re.sub(r"[^A-Za-zÁÉÍÓÚÑáéíóúñ ]", " ", value or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;.")
    return cleaned


def extract_name(text: str) -> Optional[str]:
    """Extract a probable name from text, looking for explicit cues first."""

    if not text:
        return None

    text = str(text).strip()
    if not text:
        return None

    # Pattern 1: Explicit declaration ("soy", "me llamo", etc.)
    m_explicit = re.search(
        r"(?:soy|me llamo|mi nombre es)\s+([A-Za-zÁÉÍÓÚÑáéíóúñ ]{2,60})",
        text,
        re.IGNORECASE,
    )
    if m_explicit:
        candidate = _sanitize_name_candidate(m_explicit.group(1))
        if validate_name(candidate):
            return candidate

    # Pattern 2: Label-based input ("Nombre: Juan Perez")
    m_label = re.search(
        r"(?:^|[\n,;])\s*(?:nombre(?:\s+completo)?|nom)[:\-\s]+([A-Za-zÁÉÍÓÚÑáéíóúñ ]{2,60})",
        text,
        re.IGNORECASE,
    )
    if m_label:
        candidate = _sanitize_name_candidate(m_label.group(1))
        if validate_name(candidate):
            return candidate

    # Pattern 3: Look for short segments likely containing only a name.
    segments = []
    segments.extend(filter(None, re.split(r"[\n]+", text)))
    segments.extend(filter(None, re.split(r"[,;]", text)))
    segments.append(text)

    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue
        if len(segment) > 60:
            continue
        if any(char.isdigit() for char in segment):
            continue

        words = segment.split()
        if not words or len(words) > 3:
            continue

        normalized_first = re.sub(r"^[^A-Za-zÁÉÍÓÚÑáéíóúñ]*", "", words[0]).lower().rstrip(":")
        if normalized_first in NAME_PREFIX_STOPWORDS:
            continue

        candidate = _sanitize_name_candidate(segment)
        if not candidate:
            continue

        if len(candidate) > 50 or len(candidate) < 2:
            continue

        # Avoid treating long sentences as names.
        if len(segment) > 40 and len(words) > 1:
            continue

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


def extract_dni(text: str) -> Optional[tuple[str, str]]:
    """
    Extracts an Argentine DNI (7-8 digits), ignoring dots or spaces.
    Returns a tuple of (normalized_dni, raw_match) or None.
    """
    if not text:
        return None
    # This regex looks for a sequence of digits that could be a DNI,
    # allowing for dots or spaces as separators.
    # It captures XX.XXX.XXX or X.XXX.XXX or XXXXXXXX patterns.
    match = re.search(r'\b(\d{1,2}[.\s]?\d{3}[.\s]?\d{3})\b', text)
    if match:
        raw_match = match.group(0)
        normalized = re.sub(r'\D', '', raw_match)
        if 7 <= len(normalized) <= 8:
            return normalized, raw_match
    # Fallback for numbers without separators
    match = re.search(r'\b(\d{7,8})\b', text)
    if match:
        raw_match = match.group(0)
        return raw_match, raw_match
    return None
