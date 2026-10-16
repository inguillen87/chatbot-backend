from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import TYPE_CHECKING, Dict, List, Any, Optional  # Added Optional
from utils.validators import (
    extract_email,
    extract_phone,
    extract_name,
    extract_address,
    extract_dni,
    normalize_phone,
    validate_name,
)
from utils.lazy_module import LazyModule

documentai = LazyModule("google.cloud.documentai")
vision = LazyModule("google.cloud.vision")
cohere = LazyModule("cohere")
VISION_CLIENT = None
CohereAPIError = None

if TYPE_CHECKING:
    from google.cloud.documentai_v1 import Document as Document


def __getattr__(name: str) -> Any:
    """Preserve the legacy Document export without eager provider imports."""

    if name == "Document":
        return documentai.Document
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _is_cohere_api_error(exc: Exception) -> bool:
    """Classify Cohere errors without importing its SDK during app startup."""

    global CohereAPIError
    if CohereAPIError is None:
        try:
            error_types = (
                getattr(cohere, "CohereAPIError", None),
                getattr(getattr(cohere, "errors", None), "CohereAPIError", None),
                getattr(cohere, "APIError", None),
                getattr(cohere, "CohereError", None),
            )
        except (ImportError, ModuleNotFoundError):
            error_types = ()
        CohereAPIError = next(
            (candidate for candidate in error_types if isinstance(candidate, type)),
            False,
        )
    return isinstance(exc, CohereAPIError) if isinstance(CohereAPIError, type) else False


from services.openai_text_service import robust_chat
from services.openai_model_defaults import (
    DEFAULT_OPENAI_TERRA_MODEL,
    resolve_openai_model,
)

logger = logging.getLogger(__name__)

_CONTACT_CLAUSE_PATTERN = re.compile(
    r"\b(?:mi\s+(?:nombre|n[úu]mero|documento|dni|tel[eé]fono|celular|mail|correo|email|direcci[oó]n)\s+[^.,;:\n]*|"
    r"soy\s+[A-Za-zÁÉÍÓÚÑáéíóúñ ]{2,60})",
    re.IGNORECASE,
)

_FILLER_START_WORDS = {
    "hola",
    "buenas",
    "buenos",
    "dias",
    "días",
    "tardes",
    "noches",
    "si",
    "sí",
    "mira",
    "mirá",
    "queria",
    "quería",
    "quisiera",
    "necesito",
}

_DESCRIPTION_SKIP_WORDS = _FILLER_START_WORDS | {
    "hacer",
    "reclamo",
    "reclamos",
    "consulta",
    "consultar",
    "consultas",
    "pregunta",
    "pregunto",
    "quiero",
    "queremos",
    "quisieramos",
    "quisiéramos",
    "solicito",
    "solicitamos",
    "presento",
    "presentar",
    "aviso",
    "avisar",
    "tengo",
    "tenemos",
    "hay",
    "soy",
    "somos",
}

_SUMMARY_LEADING_SKIP = _DESCRIPTION_SKIP_WORDS | {
    "favor",
    "un",
    "una",
    "por",
}

_PHONE_KEYWORD_PATTERN = re.compile(
    r"(tel[eé]fono|celular|whatsapp|contacto|llam[aé]me|llamar)", re.IGNORECASE
)

_ADDRESS_FORBIDDEN_WORDS = {
    "documento",
    "dni",
    "correo",
    "email",
    "mail",
    "celular",
    "telefono",
    "teléfono",
    "whatsapp",
}

_ADDRESS_HINT_WORDS = {
    "calle",
    "avenida",
    "av.",
    "ruta",
    "esquina",
    "barrio",
    "pasaje",
    "pje",
    "manzana",
    "lote",
    "km",
    "interseccion",
    "intersección",
    "plaza",
    "esq",
}

_NAME_DISALLOWED_TOKENS = {
    "calle",
    "avenida",
    "ruta",
    "esquina",
    "barrio",
    "pasaje",
    "pje",
    "manzana",
    "lote",
    "km",
    "interseccion",
    "intersección",
    "plaza",
    "esq",
    "frente",
    "parque",
}

_ADDRESS_PATTERNS: List[str] = [
    r"(?:mi|la|nuestra|nuestro)\s+direcci[oó]n\s+es\s+([^.;\n]+)",
    r"(?:mi|la|nuestra|nuestro)\s+ubicaci[oó]n\s+es\s+([^.;\n]+)",
    r"(?:vivo|estoy|queda|quedo|quedamos|nos\s+ubicamos|se\s+ubica|ubicado|ubicada|ubicados|ubicadas)\s+en\s+([^.;\n]+)",
    r"(?:en\s+la\s+esquina\s+de)\s+([^.;\n]+)",
    r"(?:entre\s+)?([A-Za-zÁÉÍÓÚÑáéíóúñ'\s]+\s+(?:y|e)\s+[A-Za-zÁÉÍÓÚÑáéíóúñ'\s]+)",
    r"(?:en|sobre)\s+([A-Za-zÁÉÍÓÚÑáéíóúñ'\s]+\d{1,6}(?:[A-Za-zÁÉÍÓÚÑáéíóúñ'\s,.-]*))",
]


def _strip_leading_filler_words(text: str) -> str:
    """Trim leading filler words and punctuation from a sentence."""

    if not text:
        return ""

    tokens = text.strip().split()
    idx = 0
    while idx < len(tokens):
        token = re.sub(r"^[^A-Za-zÁÉÍÓÚÑáéíóúñ0-9']+|[^A-Za-zÁÉÍÓÚÑáéíóúñ0-9']+$", "", tokens[idx])
        if token.lower() not in _SUMMARY_LEADING_SKIP:
            break
        idx += 1

    return " ".join(tokens[idx:]).strip(" ,.;:-")


def _ensure_string(value: Any) -> Optional[str]:
    """Return the first non-empty string representation for a value."""

    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        for item in value:
            candidate = _ensure_string(item)
            if candidate:
                return candidate
        return None
    return str(value).strip() or None


def _normalize_address_candidate(value: Any) -> str:
    """Normalize an address candidate by trimming common prefixes."""

    candidate = _ensure_string(value) or ""
    if not candidate:
        return ""

    candidate = re.sub(
        r"^(?:mi|la|nuestra|nuestro)\s+direcci[oó]n\s+es\s+",
        "",
        candidate,
        flags=re.IGNORECASE,
    )
    candidate = re.sub(
        r"^(?:mi|la|nuestra|nuestro)\s+ubicaci[oó]n\s+es\s+",
        "",
        candidate,
        flags=re.IGNORECASE,
    )
    candidate = re.sub(
        r"^(?:vivo|estoy|queda|quedo|quedamos|nos\s+ubicamos|se\s+ubica|ubicado|ubicada|ubicados|ubicadas)\s+en\s+",
        "",
        candidate,
        flags=re.IGNORECASE,
    )
    candidate = re.sub(
        r".*?(?:vivo|estoy|queda|quedo|quedamos|nos\s+ubicamos|se\s+ubica|ubicado|ubicada|ubicados|ubicadas)\s+en\s+",
        "",
        candidate,
        flags=re.IGNORECASE,
        count=1,
    )
    candidate = re.sub(
        r"^(?:en\s+la\s+esquina\s+de)\s+",
        "",
        candidate,
        flags=re.IGNORECASE,
    )
    candidate = re.sub(
        r"\b(?:y\s+)?mi\s+(?:n[úu]mero|tel[eé]fono|celular)\b.*",
        "",
        candidate,
        flags=re.IGNORECASE,
    )
    candidate = re.sub(
        r"\bmi\s+documento\b.*",
        "",
        candidate,
        flags=re.IGNORECASE,
    )
    candidate = re.sub(r"\b(?:hay|tengo|tenemos)\b.*", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\bque\s+(?:no|est[aá]|se)\b.*", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\b(esquina\s+)+", "esquina ", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\s+", " ", candidate)
    candidate = candidate.strip(" ,.;:-")
    return candidate


def _looks_like_address_fragment(value: Any) -> bool:
    """Heuristic to determine if a string resembles an address."""

    candidate = _normalize_address_candidate(value)
    if not candidate:
        return False

    lowered = candidate.lower()
    if any(stop_word in lowered for stop_word in _ADDRESS_FORBIDDEN_WORDS):
        return False

    if len(candidate.split()) < 2:
        return False

    has_number = bool(re.search(r"\d{1,6}", candidate))
    has_hint = any(hint in lowered for hint in _ADDRESS_HINT_WORDS)
    has_intersection = bool(
        re.search(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]+\s+(?:y|e)\s+[A-Za-zÁÉÍÓÚÑáéíóúñ]+", candidate)
    )

    return has_number or has_hint or has_intersection


def _score_address_candidate(candidate: str) -> tuple[int, int, int]:
    """Return a score tuple to sort address candidates by relevance."""

    lowered = candidate.lower()
    has_number = bool(re.search(r"\d{1,6}", candidate))
    has_intersection = "esquina" in lowered or bool(
        re.search(r"\b(?:y|e)\b", lowered)
    )
    starts_with_preposition = lowered.startswith("en ") or lowered.startswith("sobre ")

    tokens = re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñ0-9]+", candidate)
    short_tokens = sum(1 for token in tokens if token.isalpha() and len(token) <= 2)

    score = 0
    if has_number:
        score += 2
    if has_intersection:
        score += 1
    if not starts_with_preposition:
        score += 1

    return score, -short_tokens, len(candidate)


def _extract_address_candidates(text: str) -> List[str]:
    """Return a list of possible addresses detected in free text."""

    if not text:
        return []

    candidates: List[tuple[tuple[int, int, int], str]] = []
    for pattern in _ADDRESS_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            # Patterns may have capturing groups; use the first one when available.
            candidate = match.group(1) if match.groups() else match.group(0)
            candidate = _normalize_address_candidate(candidate)
            if _looks_like_address_fragment(candidate):
                candidates.append((_score_address_candidate(candidate), candidate))

    fallback = extract_address(text)
    if fallback:
        fallback = _normalize_address_candidate(fallback)
        if _looks_like_address_fragment(fallback):
            candidates.append((_score_address_candidate(fallback), fallback))

    unique_candidates: List[str] = []
    seen = set()
    for _score, candidate in sorted(candidates, key=lambda item: item[0], reverse=True):
        key = candidate.lower()
        if key not in seen:
            seen.add(key)
            unique_candidates.append(candidate)

    return unique_candidates


def _extract_phone_candidate(value: Any, region: str = "AR") -> Optional[str]:
    """Extract and normalize a phone number from a value."""

    candidate = _ensure_string(value)
    if candidate:
        matches: List[tuple[tuple[bool, bool, int], str]] = []
        for match in re.finditer(r"(\+?\d[\d\s.-]{7,18}\d)", candidate):
            raw_value = match.group(1)
            digits = re.sub(r"\D", "", raw_value)
            if len(digits) < 8:
                continue

            surrounding = candidate[max(0, match.start() - 25) : match.end() + 5]
            if re.search(r"\b(dni|documento|ticket|tramite|trámite)\b", surrounding, re.IGNORECASE):
                continue

            normalized = normalize_phone(raw_value, region=region)
            if not normalized:
                continue

            has_keyword = bool(
                _PHONE_KEYWORD_PATTERN.search(candidate[max(0, match.start() - 25) : match.start()])
            )
            is_long_number = len(digits) >= 10
            if len(digits) < 9 and not has_keyword:
                continue
            matches.append(((has_keyword, is_long_number, len(digits)), normalized))

        if matches:
            matches.sort(key=lambda item: item[0], reverse=True)
            return matches[0][1]

        phone = extract_phone(candidate, region=region)
        if phone:
            normalized, _raw = phone
            if normalized and len(re.sub(r"\D", "", normalized)) >= 9:
                return normalized
    return None


def _count_digits(value: Optional[str]) -> int:
    """Return the count of numeric digits in the provided value."""

    if not value:
        return 0
    return len(re.sub(r"\D", "", value))


def _select_best_phone_candidate(candidates: List[tuple[Optional[str], bool]]) -> Optional[str]:
    """Pick the most plausible phone number from candidate values."""

    best_value: Optional[str] = None
    best_score: tuple[int, int, int] = (-1, -1, -1)
    for value, from_text in candidates:
        if not value:
            continue
        digits_count = _count_digits(value)
        if digits_count < 9:
            continue
        score = (
            digits_count,
            1 if from_text else 0,
            1 if isinstance(value, str) and value.startswith("+") else 0,
        )
        if score > best_score:
            best_score = score
            best_value = value

    return best_value


def _select_best_address_candidate(
    llm_address: Optional[str], heuristic_address: Optional[str]
) -> Optional[str]:
    """Choose the richer address candidate between LLM and heuristic outputs."""

    normalized_llm = _normalize_address_candidate(llm_address) if llm_address else None
    normalized_heuristic = (
        _normalize_address_candidate(heuristic_address) if heuristic_address else None
    )

    def score(value: Optional[str]) -> tuple[int, int, int, int]:
        if not value:
            return (-1, -1, -1, -1)
        digits = _count_digits(value)
        lowered = value.lower()
        has_intersection = 1 if ("esquina" in lowered or re.search(r"\b(?:y|e)\b", lowered)) else 0
        tokens = re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñ0-9]+", value)
        short_tokens = sum(1 for token in tokens if token.isalpha() and len(token) <= 2)
        return (digits, has_intersection, -short_tokens, len(value))

    llm_score = score(normalized_llm)
    heuristic_score = score(normalized_heuristic)

    if heuristic_score > llm_score:
        return normalized_heuristic
    if normalized_llm:
        return normalized_llm
    return normalized_heuristic


def _normalize_dni_value(value: Any) -> Optional[str]:
    """Return a sanitized DNI string if the value looks like a DNI."""

    candidate = _ensure_string(value)
    if not candidate:
        return None

    digits = re.sub(r"\D", "", candidate)
    if 7 <= len(digits) <= 8:
        return digits
    return None


def _clean_description_text(text: str) -> str:
    """Remove contact clauses from a description while keeping the issue context."""

    if not text:
        return ""

    cleaned = _CONTACT_CLAUSE_PATTERN.sub(" ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.;:-")
    if not cleaned:
        return ""

    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    filtered_sentences: List[str] = []
    for sentence in sentences:
        normalized_sentence = sentence.strip(" ,.;:-")
        normalized_sentence = _strip_leading_filler_words(normalized_sentence)
        if not normalized_sentence:
            continue
        words = re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñ']+", normalized_sentence.lower())
        if not words:
            continue
        meaningful_words = [
            word for word in words if len(word) > 2 and word not in _DESCRIPTION_SKIP_WORDS
        ]
        if not meaningful_words:
            continue
        filtered_sentences.append(normalized_sentence)

    cleaned = " ".join(filtered_sentences).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def _build_short_description(text: str, max_words: int = 5) -> Optional[str]:
    """Return a compact summary using the first relevant words of the text."""

    if not text:
        return None

    tokens = re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñ0-9']+", text)
    if not tokens:
        return None

    summary_tokens: List[str] = []
    for token in tokens:
        lower = token.lower()
        if not summary_tokens and lower in _SUMMARY_LEADING_SKIP:
            continue
        if lower in _FILLER_START_WORDS and not summary_tokens:
            continue
        summary_tokens.append(token)
        if len(summary_tokens) >= max_words:
            break

    if not summary_tokens:
        return None

    return " ".join(summary_tokens)


def _cleanup_name_candidate(name: Any) -> Optional[str]:
    """Normalize a name candidate and drop obvious non-name fragments."""

    candidate = _ensure_string(name)
    if not candidate:
        return None

    candidate = re.sub(
        r"\s+y\s+(?:mi|mis|su|sus|el|la|los|las)\b.*",
        "",
        candidate,
        flags=re.IGNORECASE,
    )
    candidate = re.sub(r"\s+con\s+.*", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\s*,.*", "", candidate)
    candidate = re.sub(r"\s+", " ", candidate).strip(" ,.;:-")
    if not candidate:
        return None

    lowered = candidate.lower()
    if any(token in lowered for token in _NAME_DISALLOWED_TOKENS):
        return None

    if len(candidate.split()) > 4:
        return None

    if not validate_name(candidate):
        return None

    return candidate

def _clean_llm_json_output(llm_output: str) -> str:
    """Clean and attempt to repair JSON returned by an LLM."""
    if not llm_output:
        return ""

    # Remove markdown code fences (```json ... ```)
    match = re.match(r"^\s*```json\s*([\s\S]*?)\s*```\s*$", llm_output, re.DOTALL)
    cleaned_output = match.group(1) if match else llm_output

    # Extract the first JSON-like block in case the model added extra text
    block_match = re.search(r"\{[\s\S]*\}|\[[\s\S]*\]", cleaned_output)
    if block_match:
        cleaned_output = block_match.group(0)

    # Remove trailing commas before closing braces or brackets
    cleaned_output = re.sub(r",\s*(?=[}\]])", "", cleaned_output)

    # Attempt to fix truncated JSON by closing quotes/brackets
    cleaned_output = _close_open_json_structures(cleaned_output)

    return cleaned_output.strip()


def _sanitize_llm_text_output(text: str) -> str:
    """Remove JSON/code-fence wrappers from LLM text output when a plain sentence is expected."""
    if not text:
        return ""

    stripped = text.strip()
    if stripped.startswith("```"):
        match = re.match(r"^\s*```(?:json)?\s*([\s\S]*?)\s*```\s*$", stripped, re.DOTALL)
        if match:
            stripped = match.group(1).strip()

    cleaned_json = _clean_llm_json_output(stripped)
    try:
        parsed = json.loads(cleaned_json)
        if isinstance(parsed, dict):
            descripcion = parsed.get("descripcion") or parsed.get("description")
            if isinstance(descripcion, str) and descripcion.strip():
                return descripcion.strip()
    except json.JSONDecodeError:
        pass

    return stripped.strip('"').strip()


def llamar_llm_para_json_estructurado(
    system_prompt: str,
    user_prompt: str,
    model: Optional[str] = None,
) -> Optional[Dict | List]:
    """
    Calls the LLM requesting a JSON output and parses it safely.

    Args:
        system_prompt: The system prompt guiding the LLM's task.
        user_prompt: The user prompt, containing the data to be processed.
        model: Explicit model override. When omitted, uses
            ``OPENAI_CHAT_MODEL_EXTRACTION`` or the role-aware Terra default.

    Returns:
        A dictionary or list parsed from the LLM's JSON response, or None on error.
    """
    from services.llm_bridge import llamar_llm_para_generacion_texto

    resolved_model = str(model or "").strip() or resolve_openai_model(
        "OPENAI_CHAT_MODEL_EXTRACTION",
        DEFAULT_OPENAI_TERRA_MODEL,
    )
    logger.info(
        "Calling LLM for structured JSON output using model=%s",
        resolved_model,
    )
    try:
        response_text = llamar_llm_para_generacion_texto(
            system_prompt_especifico=system_prompt,
            user_prompt=user_prompt,
            temperature=0.1,  # Lower temp for more deterministic JSON extraction
            json_output=True,
            model=resolved_model
        )

        if not response_text:
            logger.error("LLM returned no text for JSON extraction.")
            return None

        # Clean and parse the response
        cleaned_json_str = _clean_llm_json_output(response_text)
        if not cleaned_json_str:
            logger.error("LLM response was empty after cleaning.")
            return None

        return json.loads(cleaned_json_str)

    except json.JSONDecodeError as exc:
        logger.error(
            "Failed to decode JSON from LLM response "
            "error_type=%s response_length=%s",
            type(exc).__name__,
            len(response_text or ""),
        )
        return None
    except Exception as exc:
        logger.error(
            "LLM JSON extraction failed error_type=%s",
            type(exc).__name__,
        )
        return None


def _close_open_json_structures(json_str: str) -> str:
    """Try to close quotes and brackets for a possibly truncated JSON string."""
    if not json_str:
        return json_str

    in_string = False
    escape = False
    stack: List[str] = []
    for ch in json_str:
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
        elif not in_string:
            if ch == '{':
                stack.append('}')
            elif ch == '[':
                stack.append(']')
            elif ch in ('}', ']') and stack and stack[-1] == ch:
                stack.pop()

    if in_string:
        json_str += '"'

    while stack:
        json_str += stack.pop()

    return json_str

def extract_multiple_contact_details_llm(text: str, potential_fields: List[str]) -> Dict[str, Any]:
    """
    Uses an LLM (with heuristics) to extract multiple contact details from a given text.

    Args:
        text: The user's input string.
        potential_fields: List of expected keys (e.g., "nombre_cliente").

    Returns:
        A dictionary with the requested fields when they can be inferred.
    """

    if not text or not potential_fields:
        return {}

    filtered_fields = [field for field in potential_fields if isinstance(field, str) and field]
    if not filtered_fields:
        return {}

    prompt = (
        "Eres un asistente amigable y eficiente. Extrae los siguientes datos de contacto del MENSAJE DEL USUARIO: "
        f"{', '.join(filtered_fields)}. "
        "Devuelve la información SOLAMENTE como un objeto JSON válido con las claves de la lista: "
        f"{filtered_fields}. Si no encuentras un dato, omite la clave en el JSON. "
        "No añadas explicaciones ni texto conversacional. Asegúrate de extraer los números de teléfono de la forma más precisa posible.\n\n"
        "Ejemplo de campos:\n"
        "- nombre_cliente: Nombre completo del cliente.\n"
        "- telefono_cliente: Número de teléfono.\n"
        "- direccion_cliente: Dirección de entrega completa.\n"
        "- email_cliente: Correo electrónico.\n\n"
        f"MENSAJE DEL USUARIO: \"{text}\"\n\n"
        "RESPUESTA JSON:"
    )

    response_content: Optional[str] = None
    raw_extracted: Dict[str, Any] = {}

    try:
        response_content = robust_chat(message=prompt)
        if response_content:
            cleaned_response = _clean_llm_json_output(response_content)
            if cleaned_response:
                parsed = json.loads(cleaned_response)
                if isinstance(parsed, dict):
                    raw_extracted = {k: parsed.get(k) for k in filtered_fields if parsed.get(k)}
                else:
                    logger.info(
                        "[LLM_CONTACT_EXTRACT] Ignoring non-dict response "
                        "response_type=%s response_length=%s input_length=%s",
                        type(parsed).__name__,
                        len(response_content or ""),
                        len(text or ""),
                    )
            else:
                logger.info(
                    "[LLM_CONTACT_EXTRACT] LLM response was empty after cleaning "
                    "response_length=%s input_length=%s",
                    len(response_content or ""),
                    len(text or ""),
                )
        else:
            logger.info(
                "[LLM_CONTACT_EXTRACT] LLM returned empty response input_length=%s",
                len(text or ""),
            )

    except json.JSONDecodeError as exc:
        logger.error(
            "[LLM_CONTACT_EXTRACT] Invalid JSON response "
            "error_type=%s response_length=%s input_length=%s",
            type(exc).__name__,
            len(response_content or ""),
            len(text or ""),
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(
            "[LLM_CONTACT_EXTRACT] Provider call failed "
            "error_type=%s input_length=%s",
            type(exc).__name__,
            len(text or ""),
        )

    extracted_data: Dict[str, Any] = {}
    for key, value in raw_extracted.items():
        sanitized = _ensure_string(value)
        if sanitized:
            extracted_data[key] = sanitized

    if "nombre_cliente" in extracted_data:
        cleaned_llm_name = _cleanup_name_candidate(extracted_data.get("nombre_cliente"))
        if cleaned_llm_name:
            extracted_data["nombre_cliente"] = cleaned_llm_name
        else:
            extracted_data.pop("nombre_cliente", None)

    normalized_text = text or ""

    if "nombre_cliente" in filtered_fields:
        llm_name = extracted_data.get("nombre_cliente")
        name_cue_present = bool(
            re.search(r"\b(soy|me llamo|mi nombre es|nombre\s*:)", normalized_text, re.IGNORECASE)
        )
        heuristic_name = _cleanup_name_candidate(
            extract_name(normalized_text) if name_cue_present else None
        )
        if heuristic_name:
            current_name = llm_name
            if not current_name:
                extracted_data["nombre_cliente"] = heuristic_name
            elif (
                heuristic_name.lower() not in current_name.lower()
                and len(heuristic_name.split()) >= len(current_name.split())
            ):
                extracted_data["nombre_cliente"] = heuristic_name
        elif not llm_name:
            extracted_data.pop("nombre_cliente", None)

    if "email_cliente" in filtered_fields:
        heuristic_email = extract_email(normalized_text)
        llm_email = _ensure_string(extracted_data.get("email_cliente"))
        if heuristic_email:
            if not llm_email or heuristic_email.lower() != llm_email.lower():
                extracted_data["email_cliente"] = heuristic_email
        elif llm_email:
            extracted_data["email_cliente"] = llm_email

    if "telefono_cliente" in filtered_fields:
        llm_phone_raw = _ensure_string(extracted_data.get("telefono_cliente"))
        llm_phone = _extract_phone_candidate(llm_phone_raw)
        text_phone = _extract_phone_candidate(normalized_text)
        best_phone = _select_best_phone_candidate(
            [
                (llm_phone, False),
                (text_phone, True),
            ]
        )
        if best_phone:
            extracted_data["telefono_cliente"] = best_phone
        elif llm_phone_raw:
            extracted_data["telefono_cliente"] = llm_phone_raw
        else:
            extracted_data.pop("telefono_cliente", None)

    if "direccion_cliente" in filtered_fields:
        llm_address = _ensure_string(extracted_data.get("direccion_cliente"))
        if llm_address and not _looks_like_address_fragment(llm_address):
            llm_address = None
            extracted_data.pop("direccion_cliente", None)
        address_candidates = _extract_address_candidates(normalized_text)
        heuristic_address = address_candidates[0] if address_candidates else None
        best_address = _select_best_address_candidate(llm_address, heuristic_address)
        if best_address:
            extracted_data["direccion_cliente"] = best_address
        else:
            extracted_data.pop("direccion_cliente", None)

    if "dni_cliente" in filtered_fields:
        llm_dni = _normalize_dni_value(extracted_data.get("dni_cliente"))
        if llm_dni:
            extracted_data["dni_cliente"] = llm_dni
        else:
            dni_match = extract_dni(normalized_text)
            if dni_match:
                extracted_data["dni_cliente"] = dni_match[0]
            else:
                extracted_data.pop("dni_cliente", None)

    final_data = {k: v for k, v in extracted_data.items() if k in filtered_fields and v}
    return final_data

def _extract_explicit_callback_request(text: str) -> bool:
    """Validate that the citizen explicitly asked to be called.

    The LLM remains responsible for understanding the multi-intent turn and
    writing the callback reason. This conservative guard only prevents an LLM
    hallucination from turning an informational phone question into an
    operational callback request.
    """

    normalized = "".join(
        character
        for character in unicodedata.normalize("NFD", str(text or "").lower())
        if not unicodedata.combining(character)
    )
    normalized = re.sub(r"[^a-z0-9\s]", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return False

    callback_patterns = (
        r"\b(?:llamame|contactame)\b",
        r"\b(?:me\s+)?(?:puedes|podes|podrias|pueden|podrian)\s+"
        r"(?:llamar|contactar)(?:me)?\b",
        r"\b(?:quiero|necesito|prefiero)\s+que\s+(?:me\s+)?"
        r"(?:llamen|contacten)\b",
        r"\bpor\s+favor\b.{0,35}\b(?:llamame|contactame|me\s+llaman|me\s+contactan)\b",
    )
    return any(re.search(pattern, normalized) for pattern in callback_patterns)


def extract_complaint_details_llm(
    text: str,
    default_localidad: str | None = None,
    default_provincia: str | None = None,
) -> Dict[str, Any]:
    """Classify intent and extract complaint details from one citizen turn.

    ``intencion`` and ``es_reclamo`` form the fail-closed intent contract used
    by automatic claim bootstrap. Python validates that both fields agree; it
    never infers claim intent from a category or description alone.
    """

    if not text:
        return {}

    location_context_instruction = ""
    if (
        default_localidad
        and default_provincia
        and default_localidad != "N/A"
        and default_provincia != "N/A"
    ):
        location_context_instruction = (
            f"Este reclamo es para el municipio de {default_localidad}, {default_provincia}. "
            "Si el usuario menciona una calle y número pero no una ciudad o provincia, "
            f"asumí que la dirección corresponde a {default_localidad}, {default_provincia}. "
            "Solo usa estos valores por defecto para localidad y provincia si el usuario NO los especifica."
        )
    elif default_localidad and default_localidad != "N/A":
        location_context_instruction = (
            f"Este reclamo es para el municipio de {default_localidad}. "
            "Si el usuario menciona una calle y número pero no una ciudad, "
            f"asumí que la dirección corresponde a {default_localidad}. "
            "Solo usa este valor por defecto para localidad si el usuario NO lo especifica."
        )

    prompt = (
        "Eres un asistente municipal amable y comprensivo. Primero clasifica la intención del MENSAJE DEL USUARIO y luego extrae datos si corresponde: "
        "1. 'intencion': usa exactamente 'crear_reclamo' si la persona reporta un problema concreto y pide o implica acción municipal; "
        "usa 'consulta_informativa' si pregunta por áreas, responsabilidades, requisitos, horarios o información sin reportar un incidente propio; "
        "usa 'ambiguo' si no hay evidencia suficiente para decidir. "
        "2. 'es_reclamo': booleano JSON true solamente cuando 'intencion' sea 'crear_reclamo'; false en los demás casos. "
        "Una categoría municipal mencionada dentro de una pregunta informativa NO convierte el mensaje en reclamo. "
        "3. 'tipo_problema': La categoría general del problema (ej: 'Alumbrado público', 'Recolección de residuos', 'Fuga de agua'). "
        "4. 'ubicacion_problema': El lugar específico del problema (calle, número, etc.). "
        "5. 'descripcion_problema': Un resumen claro y conciso del reclamo. "
        "6. 'descripcion_corta': Una descripción muy breve del problema, de no más de 5 palabras (ej: 'Basura en la zanja', 'Poste de luz caído'). "
        "7. 'nombre_cliente': El nombre de la persona que reclama, si lo menciona. "
        "8. 'email_cliente': El email de la persona, si lo menciona. NOTA: A veces, la transcripción de audio confunde '@' con un punto ('.'). Si ves algo como 'usuario.dominio.com', es muy probable que sea 'usuario@dominio.com'. "
        "9. 'telefono_cliente': El teléfono de la persona, si lo menciona. "
        "10. 'dni_cliente': El DNI de la persona, si lo menciona. "
        "11. 'solicita_llamada': true solamente si la persona pide explícitamente que la llamen o contacten por teléfono; en caso contrario false. "
        "12. 'motivo_llamada': Un motivo breve y operativo para la llamada, solo cuando 'solicita_llamada' sea true. "
        f"{location_context_instruction} "
        "Devuelve la información SOLAMENTE como un objeto JSON válido con estas claves. "
        "Si no encuentras un detalle, puedes omitir la clave. "
        "No añadas explicaciones ni texto conversacional.\n\n"
        f"MENSAJE DEL USUARIO: \"{text}\"\n\n"
        "RESPUESTA JSON:"
    )

    response_content: Optional[str] = None
    llm_result: Dict[str, Any] = {}
    valid_keys = [
        "intencion",
        "es_reclamo",
        "tipo_problema",
        "ubicacion_problema",
        "descripcion_problema",
        "descripcion_corta",
        "nombre_cliente",
        "email_cliente",
        "telefono_cliente",
        "dni_cliente",
        "solicita_llamada",
        "motivo_llamada",
    ]

    try:
        response_content = robust_chat(message=prompt)
        if response_content:
            cleaned_response = _clean_llm_json_output(response_content)
            if cleaned_response:
                parsed = json.loads(cleaned_response)
                if isinstance(parsed, dict):
                    for key in valid_keys:
                        if key not in parsed:
                            continue
                        value = parsed.get(key)
                        if key == "es_reclamo":
                            if isinstance(value, bool):
                                llm_result[key] = value
                            continue
                        if key == "intencion":
                            normalized_intent = str(value or "").strip().lower()
                            if normalized_intent in {
                                "crear_reclamo",
                                "consulta_informativa",
                                "ambiguo",
                            }:
                                llm_result[key] = normalized_intent
                            continue
                        if key == "solicita_llamada":
                            if value is True or (
                                isinstance(value, str)
                                and value.strip().lower() in {"true", "1", "si", "sí"}
                            ):
                                llm_result[key] = True
                            continue
                        if key == "descripcion_problema" and isinstance(value, (list, tuple)):
                            combined = " ".join(
                                filter(None, (_ensure_string(item) for item in value))
                            ).strip()
                            sanitized = combined or None
                        else:
                            sanitized = _ensure_string(value)
                        if sanitized:
                            llm_result[key] = sanitized
                else:
                    logger.info(
                        "[LLM_COMPLAINT_EXTRACT] Ignoring non-dict response "
                        "response_type=%s response_length=%s input_length=%s",
                        type(parsed).__name__,
                        len(response_content or ""),
                        len(text or ""),
                    )
            else:
                logger.info(
                    "[LLM_COMPLAINT_EXTRACT] LLM response was empty after cleaning "
                    "response_length=%s input_length=%s",
                    len(response_content or ""),
                    len(text or ""),
                )
        else:
            logger.info(
                "[LLM_COMPLAINT_EXTRACT] LLM returned empty response input_length=%s",
                len(text or ""),
            )

    except json.JSONDecodeError as exc:
        logger.info(
            "[LLM_COMPLAINT_EXTRACT] Invalid JSON response; using fallback "
            "error_type=%s response_length=%s input_length=%s",
            type(exc).__name__,
            len(response_content or ""),
            len(text or ""),
        )
    except Exception as exc:  # pragma: no cover - defensive
        provider = "cohere" if _is_cohere_api_error(exc) else "llm"
        logger.error(
            "[LLM_COMPLAINT_EXTRACT] Provider call failed; using fallback "
            "provider=%s error_type=%s input_length=%s",
            provider,
            type(exc).__name__,
            len(text or ""),
        )

    result: Dict[str, Any] = {
        key: value
        for key, value in llm_result.items()
        if value or (key == "es_reclamo" and isinstance(value, bool))
    }

    # Fail closed on incomplete or contradictory model output. These fields
    # are deliberately not synthesized when both are absent so callers can
    # distinguish a provider/schema failure from an explicit non-claim.
    if "intencion" in result or "es_reclamo" in result:
        intent = result.get("intencion")
        is_claim = result.get("es_reclamo")
        contract_is_consistent = (
            intent == "crear_reclamo" and is_claim is True
        ) or (
            intent in {"consulta_informativa", "ambiguo"} and is_claim is False
        )
        if not contract_is_consistent:
            result["intencion"] = "ambiguo"
            result["es_reclamo"] = False
    normalized_text = text or ""

    explicit_callback_request = _extract_explicit_callback_request(normalized_text)
    if explicit_callback_request:
        result["solicita_llamada"] = True
        callback_reason = _ensure_string(result.get("motivo_llamada"))
        result["motivo_llamada"] = (
            callback_reason[:500]
            if callback_reason
            else "La persona solicitó ser contactada por teléfono por este reclamo."
        )
    else:
        # Python validates the operational side effect: the model cannot invent
        # a callback request that the citizen did not make.
        result.pop("solicita_llamada", None)
        result.pop("motivo_llamada", None)

    if "nombre_cliente" in result:
        cleaned_name = _cleanup_name_candidate(result.get("nombre_cliente"))
        if cleaned_name:
            result["nombre_cliente"] = cleaned_name
        else:
            result.pop("nombre_cliente", None)

    name_cue_present = bool(
        re.search(r"\b(soy|me llamo|mi nombre es|nombre\s*:)", normalized_text, re.IGNORECASE)
    )
    heuristic_name = _cleanup_name_candidate(
        extract_name(normalized_text) if name_cue_present else None
    )
    if heuristic_name:
        current_name = result.get("nombre_cliente")
        if not current_name:
            result["nombre_cliente"] = heuristic_name
        elif (
            heuristic_name.lower() not in current_name.lower()
            and len(heuristic_name.split()) >= len(current_name.split())
        ):
            result["nombre_cliente"] = heuristic_name

    heuristic_email = extract_email(normalized_text)
    if heuristic_email:
        current_email = result.get("email_cliente")
        if not current_email or heuristic_email.lower() != current_email.lower():
            result["email_cliente"] = heuristic_email

    llm_phone_raw = _ensure_string(result.get("telefono_cliente"))
    llm_phone = _extract_phone_candidate(llm_phone_raw)
    heuristic_phone = _extract_phone_candidate(normalized_text)
    best_phone = _select_best_phone_candidate(
        [
            (llm_phone, False),
            (heuristic_phone, True),
        ]
    )
    if best_phone:
        result["telefono_cliente"] = best_phone
    elif llm_phone_raw:
        result["telefono_cliente"] = llm_phone_raw
    else:
        result.pop("telefono_cliente", None)

    if result.get("dni_cliente"):
        normalized_dni = _normalize_dni_value(result["dni_cliente"])
        if normalized_dni:
            result["dni_cliente"] = normalized_dni
        else:
            result.pop("dni_cliente", None)
    dni_match = extract_dni(normalized_text)
    if dni_match:
        result["dni_cliente"] = dni_match[0]

    if result.get("ubicacion_problema") and not _looks_like_address_fragment(
        result["ubicacion_problema"]
    ):
        result.pop("ubicacion_problema", None)

    address_candidates = _extract_address_candidates(normalized_text)
    heur_address: Optional[str] = None
    if address_candidates:
        heur_address = _normalize_address_candidate(address_candidates[0])
        if heur_address and _looks_like_address_fragment(heur_address):
            if (
                default_localidad
                and default_localidad != "N/A"
                and default_localidad.lower() not in heur_address.lower()
            ):
                heur_address = f"{heur_address}, {default_localidad}"
            if (
                default_provincia
                and default_provincia != "N/A"
                and default_provincia.lower() not in heur_address.lower()
            ):
                heur_address = f"{heur_address}, {default_provincia}"
        else:
            heur_address = None

    if heur_address or result.get("ubicacion_problema"):
        best_address = _select_best_address_candidate(
            result.get("ubicacion_problema"), heur_address
        )
        if best_address:
            enriched_address = best_address
            if (
                default_localidad
                and default_localidad != "N/A"
                and default_localidad.lower() not in enriched_address.lower()
            ):
                enriched_address = f"{enriched_address}, {default_localidad}"
            if (
                default_provincia
                and default_provincia != "N/A"
                and default_provincia.lower() not in enriched_address.lower()
            ):
                enriched_address = f"{enriched_address}, {default_provincia}"

            result["ubicacion_problema"] = enriched_address
        else:
            result.pop("ubicacion_problema", None)

    description_candidate = _ensure_string(result.get("descripcion_problema")) or ""
    cleaned_description = _clean_description_text(description_candidate)
    if not cleaned_description or len(cleaned_description.split()) < 4:
        cleaned_description = _clean_description_text(normalized_text)
    if cleaned_description:
        result["descripcion_problema"] = cleaned_description
    else:
        result.pop("descripcion_problema", None)

    llm_short = _ensure_string(result.get("descripcion_corta"))
    short_source = result.get("descripcion_problema") or normalized_text
    heuristic_short = _build_short_description(short_source)
    if heuristic_short:
        result["descripcion_corta"] = heuristic_short
    elif llm_short:
        rebuilt_short = _build_short_description(llm_short)
        if rebuilt_short:
            result["descripcion_corta"] = rebuilt_short
        else:
            result["descripcion_corta"] = llm_short
    else:
        result.pop("descripcion_corta", None)

    result = {
        key: value
        for key, value in result.items()
        if value or (key == "es_reclamo" and isinstance(value, bool))
    }
    return result

def update_summary_with_llm_extraction(current_summary: str, extracted_data: Dict[str, Any]) -> str:
    """
    Updates an existing summary string with new information extracted by an LLM.
    This is a simplified version; a more sophisticated approach might involve an LLM call
    to intelligently merge the information.

    Args:
        current_summary: The existing summary string.
        extracted_data: A dictionary of new data to incorporate.

    Returns:
        The updated summary string.
    """
    if not extracted_data:
        return current_summary

    # For this version, we'll use a simple LLM call to re-summarize if the robust_chat is real.
    # If using the mock, it will do a basic append.

    prompt = (
        "You are a text summarization assistant. Given a CURRENT SUMMARY and NEW DATA, "
        "intelligently update the summary. Integrate the new data naturally, avoid redundancy, "
        "and maintain clarity. If new data contradicts or refines existing points, reflect that. "
        "Return only the updated summary text. \n\n"
        f"CURRENT SUMMARY: '''{current_summary}'''\n\n"
        f"NEW DATA (in JSON format): '''{json.dumps(extracted_data, indent=2)}'''\n\n"
        "UPDATED SUMMARY:"
    )
    try:
        updated_summary = robust_chat(message=prompt) # Removed model_override
        if updated_summary:
            return updated_summary.strip()
        else: # Fallback if LLM returns empty
            logger.warning("[LLM_UPDATE_SUMMARY] LLM returned empty for summary update. Using basic append.")
    except Exception as exc:
        logger.error(
            "[LLM_UPDATE_SUMMARY] Provider call failed; using basic append "
            "error_type=%s",
            type(exc).__name__,
        )
        # Fall through to basic append on error

    # Basic append logic (fallback or if mock is used)
    summary_lines = [current_summary] if current_summary else []
    for key, value in extracted_data.items():
        if value: # Only add if there's a value
            # Try to make keys more readable
            readable_key = key.replace("_", " ").capitalize()
            # Avoid adding duplicate lines if the info seems to be already there (very basic check)
            if f"{readable_key}: {value}" not in current_summary:
                 summary_lines.append(f"- {readable_key}: {value}")

    return "\n".join(filter(None, summary_lines))


if __name__ == '__main__':
    # Basic Test Examples (run this file directly to test)
    logging.basicConfig(level=logging.INFO)

    print("\n--- Testing extract_multiple_contact_details_llm ---")
    test_contact_text_1 = "Hola, soy Juan Pérez y mi teléfono es 11-5555-1234. Vivo en Av. Siempre Viva 742. Mi email es juan.perez@example.com"
    fields_to_get = ["nombre_cliente", "telefono_cliente", "direccion_cliente", "email_cliente", "otro_campo_no_presente"]
    contact_details_1 = extract_multiple_contact_details_llm(test_contact_text_1, fields_to_get)
    print(f"Input: \"{test_contact_text_1}\"\nExtracted: {json.dumps(contact_details_1, indent=2, ensure_ascii=False)}")

    test_contact_text_2 = "Necesito ayuda. Soy Ana Gómez."
    contact_details_2 = extract_multiple_contact_details_llm(test_contact_text_2, ["nombre_cliente", "telefono_cliente"])
    print(f"Input: \"{test_contact_text_2}\"\nExtracted: {json.dumps(contact_details_2, indent=2, ensure_ascii=False)}")

    test_contact_text_3 = "Mi dirección es Falsa 123 y mi teléfono 98765432."
    contact_details_3 = extract_multiple_contact_details_llm(test_contact_text_3, ["direccion_cliente", "telefono_cliente"])
    print(f"Input: \"{test_contact_text_3}\"\nExtracted: {json.dumps(contact_details_3, indent=2, ensure_ascii=False)}")

    print("\n--- Testing extract_complaint_details_llm ---")
    test_complaint_text_1 = "Hay una farola rota en la esquina de Calle Falsa y Avenida Verdadera, no funciona desde ayer. Es un peligro."
    complaint_details_1 = extract_complaint_details_llm(test_complaint_text_1)
    print(f"Input: \"{test_complaint_text_1}\"\nExtracted: {json.dumps(complaint_details_1, indent=2, ensure_ascii=False)}")

    test_complaint_text_2 = "Los vecinos del departamento 3B hacen mucho ruido todas las noches con música alta."
    complaint_details_2 = extract_complaint_details_llm(test_complaint_text_2)
    print(f"Input: \"{test_complaint_text_2}\"\nExtracted: {json.dumps(complaint_details_2, indent=2, ensure_ascii=False)}")

    print("\n--- Testing update_summary_with_llm_extraction ---")
    summary1 = "El cliente reportó un problema con el servicio."
    data1 = {"tipo_problema": "Corte de suministro", "duracion_estimada": "2 horas"}
    updated_summary1 = update_summary_with_llm_extraction(summary1, data1)
    print(f"Original Summary: \"{summary1}\"\nNew Data: {data1}\nUpdated Summary: \"{updated_summary1}\"")

    summary2 = ""
    data2 = {"nombre_cliente": "Pedro Paramo", "estado_pedido": "En preparación"}
    updated_summary2 = update_summary_with_llm_extraction(summary2, data2)
    print(f"Original Summary: \"{summary2}\"\nNew Data: {data2}\nUpdated Summary: \"{updated_summary2}\"")

    summary3 = "Resumen existente:\n- Nombre: Juan"
    data3 = {"telefono_cliente": "12345", "Nombre": "Juan Perez"} # Test case sensitivity and overwrite logic (basic)
    updated_summary3 = update_summary_with_llm_extraction(summary3, data3)
    print(f"Original Summary: \"{summary3}\"\nNew Data: {data3}\nUpdated Summary: \"{updated_summary3}\"")

    # Test with mock robust_chat (if it's the one from this file)
    print("\n--- Testing with MOCK robust_chat (if active) ---")
    # This relies on the mock robust_chat defined at the top if services.cohere_ai is not found
    mock_contact_text = "My name is John Doe, I live at 123 Main St, call 555-1234, email john.doe@example.com"
    mock_contact_details = extract_multiple_contact_details_llm(mock_contact_text, ["nombre_cliente", "direccion_cliente", "telefono_cliente", "email_cliente"])
    print(f"Mock Input: \"{mock_contact_text}\"\nMock Extracted: {json.dumps(mock_contact_details, indent=2)}")

    mock_complaint_text = "There's a broken streetlight on Elm Street near pole 123. It's been out for 3 days."
    mock_complaint_details = extract_complaint_details_llm(mock_complaint_text)
    print(f"Mock Input: \"{mock_complaint_text}\"\nMock Extracted: {json.dumps(mock_complaint_details, indent=2)}")

    mock_summary = "Current summary: '''Initial problem reported.'''"
    mock_new_data = {"ubicacion_problema": "Calle Falsa 123", "urgencia": "Alta"}
    # Need to simulate the prompt structure for the mock robust_chat for summary
    is_mock_chat_active = hasattr(robust_chat, '__module__') and robust_chat.__module__ == __name__
    if is_mock_chat_active:
        print(f"Mock robust_chat is active: {is_mock_chat_active}")
        # The update_summary_with_llm_extraction function itself calls robust_chat,
        # so if the mock is active, it will be used.
        updated_mock_summary = update_summary_with_llm_extraction("Initial problem reported.", mock_new_data)
        print(f"Mock Summary Update:\nOriginal: Initial problem reported.\nData: {mock_new_data}\nUpdated: \"{updated_mock_summary}\"")
    else:
        print("Real robust_chat is active (or mock is not from this file). Summary test might differ.")

    # Test _clean_llm_json_output
    print("\n--- Testing _clean_llm_json_output ---")
    json_with_markdown = "```json\n{\"key\": \"value\", \"another_key\": 123,}\n```"
    cleaned_md = _clean_llm_json_output(json_with_markdown)
    print(f"Original: '{json_with_markdown}'\nCleaned: '{cleaned_md}' -> Parsed: {json.loads(cleaned_md)}")

    json_with_trailing_comma = "{\"name\": \"Test\", \"items\": [1, 2,], \"valid\": true,}"
    cleaned_tc = _clean_llm_json_output(json_with_trailing_comma)
    print(f"Original: '{json_with_trailing_comma}'\nCleaned: '{cleaned_tc}' -> Parsed: {json.loads(cleaned_tc)}")

    json_plain = "{\"key\": \"value\"}"
    cleaned_plain = _clean_llm_json_output(json_plain)
    print(f"Original: '{json_plain}'\nCleaned: '{cleaned_plain}' -> Parsed: {json.loads(cleaned_plain)}")

    empty_json = ""
    cleaned_empty = _clean_llm_json_output(empty_json)
    print(f"Original: '{empty_json}'\nCleaned: '{cleaned_empty}'")

    null_json = None
    # cleaned_null = _clean_llm_json_output(null_json) # This would cause error, handled by function
    # print(f"Original: '{null_json}'\nCleaned: '{cleaned_null}'")


    # Test case for extract_multiple_contact_details_llm where LLM might return non-requested fields
    text_with_extra_info = "My name is Alice Wonderland, my phone is 555-0000, and my favorite color is blue."
    fields_for_alice = ["nombre_cliente", "telefono_cliente"]
    alice_details = extract_multiple_contact_details_llm(text_with_extra_info, fields_for_alice)
    print(f"Input: \"{text_with_extra_info}\"\nFields: {fields_for_alice}\nExtracted: {json.dumps(alice_details, indent=2, ensure_ascii=False)}")
    assert "favorite_color" not in alice_details # Assuming LLM might include it if not instructed well

    # Test case for extract_complaint_details_llm with fewer details
    complaint_less_detail = "El agua no sale en mi casa."
    complaint_details_less = extract_complaint_details_llm(complaint_less_detail)
    print(f"Input: \"{complaint_less_detail}\"\nExtracted: {json.dumps(complaint_details_less, indent=2, ensure_ascii=False)}")


def clasificar_entidad_con_llm(texto_usuario: str) -> str:
    """
    Clasifica el texto del usuario para determinar si se refiere a un municipio/gobierno,
    una pyme, o un ID/código.

    Args:
        texto_usuario: El texto a clasificar (puede ser un rubro, una pregunta, etc.).

    Returns:
        Una cadena que puede ser "municipio", "pyme", "id" o "desconocido".
    """
    if not texto_usuario or not texto_usuario.strip():
        return "desconocido"

    # Heurística simple para detectar posibles IDs
    # Coincide con secuencias alfanuméricas con guiones/números, o secuencias de solo números de 5+ dígitos.
    if re.match(r'^[a-zA-Z0-9-_]{6,}$', texto_usuario.strip()) or re.match(r'^\d{5,}$', texto_usuario.strip()):
        # Podríamos hacer una comprobación más sofisticada, pero esto cubre muchos casos.
        # Si parece un ID, podemos clasificarlo directamente para ahorrar una llamada al LLM.
        # Opcional: podríamos pasar esto al LLM para una doble verificación si es necesario.
        # Por ahora, si parece un ID, lo tratamos como tal.
        # Sin embargo, para cumplir el requisito de usar el LLM, lo pasaremos al prompt.
        pass # Dejamos que el LLM decida

    from services.llm_bridge import llamar_llm_para_generacion_texto

    system_prompt = (
        "Eres un clasificador de texto experto. Tu tarea es analizar el TEXTO DE ENTRADA "
        "y determinar a qué categoría pertenece: 'municipio', 'pyme', o 'id'.\n"
        "Responde única y exclusivamente con una de esas tres palabras en minúsculas.\n\n"
        "- 'municipio': Usa esta categoría si el texto se refiere a entidades gubernamentales, "
        "municipalidades, ayuntamientos, ONGs, servicios públicos (como hospitales públicos, "
        "policía, bomberos), o trámites y reclamos típicamente asociados a un ciudadano y su gobierno local.\n"
        "- 'pyme': Usa esta categoría si el texto se refiere a un negocio privado, una pequeña o mediana empresa, "
        "una tienda, un profesional independiente, o actividades comerciales como ventas, "
        "pedidos, catálogos de productos, etc.\n"
        "- 'id': Usa esta categoría si el texto parece ser un identificador único, un código de seguimiento, "
        "un número de ticket, un CUIT/CUIL, un DNI, o cualquier cadena alfanumérica que no describa "
        "una entidad sino que la identifique de forma unívoca.\n\n"
        "Ejemplos:\n"
        "Texto: 'limpieza de calles' -> municipio\n"
        "Texto: 'venta de zapatos' -> pyme\n"
        "Texto: 'consultar estado del ticket 987-ABCD' -> id\n"
        "Texto: 'Municipalidad de Las Heras' -> municipio\n"
        "Texto: 'Peluquería de María' -> pyme\n"
        "Texto: '20-34567890-1' -> id\n"
        "Texto: 'quiero hacer un reclamo' -> municipio\n"
        "Texto: 'ver el catálogo de productos' -> pyme\n"
    )

    user_prompt = f"TEXTO DE ENTRADA: \"{texto_usuario}\"\n\nCATEGORÍA:"

    try:
        logger.info(
            "[LLM_CLASIFICAR_ENTIDAD] Clasificando input_length=%s",
            len(str(texto_usuario or "")),
        )
        respuesta_raw = llamar_llm_para_generacion_texto(
            system_prompt_especifico=system_prompt,
            user_prompt=user_prompt,
            temperature=0.0 # Máxima precisión
        )

        if respuesta_raw:
            respuesta = respuesta_raw.strip().lower()
            if respuesta in ["municipio", "pyme", "id"]:
                logger.info(
                    "[LLM_CLASIFICAR_ENTIDAD] Clasificacion completada result=%s",
                    respuesta,
                )
                return respuesta
            else:
                logger.warning(
                    "[LLM_CLASIFICAR_ENTIDAD] Respuesta inesperada; "
                    "response_length=%s result=desconocido",
                    len(respuesta),
                )
                return "desconocido"
        else:
            logger.warning(
                "[LLM_CLASIFICAR_ENTIDAD] Respuesta vacia; "
                "input_length=%s result=desconocido",
                len(str(texto_usuario or "")),
            )
            return "desconocido"

    except Exception as exc:
        logger.error(
            "[LLM_CLASIFICAR_ENTIDAD] Provider call failed error_type=%s",
            type(exc).__name__,
        )
        return "desconocido"

    # Test update_summary_with_llm_extraction with more complex existing summary
    summary_complex = "Cliente: Maria Soler\nTicket: #12345\nProblema: Internet lento."
    data_complex = {"ubicacion_problema": "Oficina central", "velocidad_reportada": "1 Mbps"}
    updated_summary_complex = update_summary_with_llm_extraction(summary_complex, data_complex)
    print(f"Original Summary: \"{summary_complex}\"\nNew Data: {data_complex}\nUpdated Summary: \"{updated_summary_complex}\"")

    # Test contact extraction where user provides only partial data matching potential_fields
    partial_contact_text = "Mi teléfono es 222-3333."
    partial_fields = ["nombre_cliente", "telefono_cliente", "direccion_cliente"]
    partial_details = extract_multiple_contact_details_llm(partial_contact_text, partial_fields)
    print(f"Input: \"{partial_contact_text}\"\nFields: {partial_fields}\nExtracted: {json.dumps(partial_details, indent=2, ensure_ascii=False)}")
    # Expected: {"telefono_cliente": "222-3333"}

    # Test complaint extraction where LLM might return extra fields
    complaint_with_extra = "La basura no se recoge en Calle Luna, esquina Sol. Además, el camión hace mucho ruido."
    # (Assuming LLM might try to add "nivel_ruido": "alto" if not well-instructed)
    complaint_details_extra = extract_complaint_details_llm(complaint_with_extra)
    print(f"Input: \"{complaint_with_extra}\"\nExtracted: {json.dumps(complaint_details_extra, indent=2, ensure_ascii=False)}")
    assert "nivel_ruido" not in complaint_details_extra # Check that only expected keys are present

    # Test summary update where new data is empty
    summary_no_new_data = "Existing info."
    data_empty = {}
    updated_summary_no_new = update_summary_with_llm_extraction(summary_no_new_data, data_empty)
    print(f"Original Summary: \"{summary_no_new_data}\"\nNew Data: {data_empty}\nUpdated Summary: \"{updated_summary_no_new}\"")
    assert updated_summary_no_new == summary_no_new_data

    # Test summary update where current summary is empty
    summary_empty_current = ""
    data_for_empty_summary = {"info_inicial": "Primer dato"}
    updated_summary_empty_curr = update_summary_with_llm_extraction(summary_empty_current, data_for_empty_summary)
    print(f"Original Summary: \"{summary_empty_current}\"\nNew Data: {data_for_empty_summary}\nUpdated Summary: \"{updated_summary_empty_curr}\"")
    # Expected (for basic append): "- Info_inicial: Primer dato" or similar

    # Test _clean_llm_json_output with problematic JSON string
    bad_json_str = "```json\n{\n  \"name\": \"Test Product\",\n  \"price\": 29.99 // This is a comment\n  \"available\": true,\n}\n```"
    # Note: _clean_llm_json_output does not remove comments. JSON.loads will fail.
    # This test is more about markdown and trailing commas.
    # For comments, a more sophisticated cleaning or a more robust JSON parser would be needed.
    # Current _clean_llm_json_output will produce:
    # {"name": "Test Product", "price": 29.99 // This is a comment "available": true}
    # which is invalid JSON.
    # A better LLM prompt should ask for strictly JSON, no comments.

    # Cleaned version for testing just markdown and trailing comma:
    json_for_cleaner_test = "```json\n{\n  \"name\": \"Test Product\",\n  \"price\": 29.99,\n  \"available\": true,\n}\n```"
    cleaned_for_test = _clean_llm_json_output(json_for_cleaner_test)
    print(f"Original for cleaner: '{json_for_cleaner_test}'\nCleaned: '{cleaned_for_test}'")
    try:
        parsed_cleaned = json.loads(cleaned_for_test)
        print(f"Parsed successfully: {parsed_cleaned}")
    except json.JSONDecodeError as e:
        print(f"Failed to parse cleaned JSON: {e}")

    # Test with a more complex trailing comma scenario
    complex_trailing_comma = "{\"a\":1, \"b\":[{\"c\":2,}, {\"d\":3,}], \"e\":{\"f\":4,},}"
    cleaned_complex_tc = _clean_llm_json_output(complex_trailing_comma)
    print(f"Original complex TC: '{complex_trailing_comma}'\nCleaned: '{cleaned_complex_tc}'")
    try:
        parsed_complex_tc = json.loads(cleaned_complex_tc)
        print(f"Parsed successfully: {parsed_complex_tc}")
    except json.JSONDecodeError as e:
        print(f"Failed to parse cleaned complex TC JSON: {e}")


def generar_descripcion_natural_de_imagen(elementos: str) -> str:
    """
    Genera una descripción natural a partir de una lista de elementos detectados en una imagen.
    """
    if not elementos:
        return "No se detectaron elementos visuales."

    from services.llm_bridge import llamar_llm_para_generacion_texto

    prompt = (
        "Eres un asistente que describe imágenes de forma natural para un reporte. "
        "Basado en los siguientes elementos detectados en una imagen, genera una descripción concisa en una sola oración. "
        "No uses la frase 'En la imagen se observa'. Comienza directamente con la descripción.\n\n"
        f"Elementos detectados: '{elementos}'\n\n"
        "Descripción en una oración:"
    )
    try:
        descripcion = llamar_llm_para_generacion_texto(
            system_prompt_especifico="Genera una descripción de imagen en una oración.",
            user_prompt=prompt,
            temperature=0.5
        )
        if not descripcion:
            return elementos
        return _sanitize_llm_text_output(descripcion)
    except Exception as exc:
        logger.error(
            "Image description generation failed error_type=%s input_length=%s",
            type(exc).__name__,
            len(elementos or ""),
        )
        return elementos # Fallback a los elementos crudos


def extraer_lista_pedido_de_texto_con_llm(texto_ocr: str, pyme_id_context: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Utiliza un LLM para extraer una lista de productos y cantidades de un texto OCR.
    Intenta ser robusto a errores comunes de OCR y formatos de lista variados.

    Args:
        texto_ocr: El texto completo extraído por OCR de una imagen de pedido.
        pyme_id_context: Opcional, ID de la PYME para dar más contexto al LLM si es útil.

    Returns:
        Una lista de diccionarios, donde cada diccionario representa un item del pedido.
        Ej: [{"nombre_producto_ocr": "Coca Cola 2L", "cantidad_ocr": 2, "unidad_ocr": "botellas"}, ...]
        Retorna lista vacía si no se pueden extraer items o en caso de error.
    """
    logger_llm_utils = logging.getLogger(__name__)
    if not texto_ocr or not texto_ocr.strip():
        logger_llm_utils.warning("[LLM_PEDIDO_EXTRACT] texto_ocr vacío o solo espacios.")
        return []

    from services.llm_bridge import llamar_llm_para_generacion_texto # Local import

    # TODO: Refinar este prompt
    system_prompt_pedido = (
        "Eres un asistente experto en procesar listas de pedidos escritas a mano o en tickets. "
        "Dada la siguiente lista de productos extraída por OCR, identifica cada producto, su cantidad numérica y la unidad de medida si se especifica explícitamente (ej. kg, gr, lts, ml, caja, paquete, docena, etc.). "
        "Si la unidad es implícita o genérica como 'unidades' o 'ítems', puedes omitir el campo 'unidad_ocr'. "
        "Devuelve SOLAMENTE un array JSON de objetos. Cada objeto debe tener:\n"
        "- \"nombre_producto_ocr\": El nombre descriptivo y completo del producto tal como aparece (string).\n"
        "- \"cantidad_ocr\": La cantidad NUMÉRICA asociada al producto (integer o float).\n"
        "- \"unidad_ocr\" (opcional): La unidad de medida específica si se menciona (string).\n"
        "Si un ítem no tiene cantidad clara, intenta inferir 1. Si no puedes determinar un producto o cantidad para una línea, omítela de la lista.\n"
        "Ejemplo de salida: [{\"nombre_producto_ocr\": \"Coca Cola 2L\", \"cantidad_ocr\": 2, \"unidad_ocr\": \"botellas\"}, {\"nombre_producto_ocr\": \"Papas Fritas Grandes\", \"cantidad_ocr\": 1}]"
    )
    user_prompt_pedido = (
        "Por favor, procesa el siguiente texto OCR de un pedido y extrae los items en formato JSON array:\n"
        "Texto OCR:\n"
        "----------\n"
        f"{texto_ocr}\n"
        "----------\n"
        "Array JSON:"
    )

    logger_llm_utils.info(
        "[LLM_PEDIDO_EXTRACT] Calling provider input_length=%s has_pyme_context=%s",
        len(texto_ocr),
        pyme_id_context is not None,
    )
    respuesta_llm_texto = llamar_llm_para_generacion_texto(
        system_prompt_especifico=system_prompt_pedido,
        user_prompt=user_prompt_pedido,
        temperature=0.1 # Más determinista para extracción
    )

    if not respuesta_llm_texto:
        logger_llm_utils.warning(
            "[LLM_PEDIDO_EXTRACT] Provider returned empty response input_length=%s",
            len(texto_ocr),
        )
        return []

    cleaned_json_str = _clean_llm_json_output(respuesta_llm_texto)
    try:
        items_extraidos = json.loads(cleaned_json_str)
        if isinstance(items_extraidos, list):
            # Validar estructura de cada item
            items_validos = []
            for item in items_extraidos:
                if isinstance(item, dict) and "nombre_producto_ocr" in item and "cantidad_ocr" in item:
                    try:
                        item["cantidad_ocr"] = int(float(str(item["cantidad_ocr"]).replace(',','.'))) # Asegurar que sea int o float y luego int
                        if item["cantidad_ocr"] < 0 : item["cantidad_ocr"] = 1 # No permitir cantidades negativas
                    except ValueError:
                        item["cantidad_ocr"] = 1 # Default si la cantidad no es numérica
                    items_validos.append(item)
                else:
                    logger_llm_utils.warning(
                        "[LLM_PEDIDO_EXTRACT] Provider item missing required fields "
                        "item_type=%s has_name=%s has_quantity=%s",
                        type(item).__name__,
                        isinstance(item, dict) and "nombre_producto_ocr" in item,
                        isinstance(item, dict) and "cantidad_ocr" in item,
                    )
            logger_llm_utils.info(
                "[LLM_PEDIDO_EXTRACT] Extraction completed valid_item_count=%s "
                "response_length=%s input_length=%s",
                len(items_validos),
                len(respuesta_llm_texto),
                len(texto_ocr),
            )
            return items_validos
        else:
            logger_llm_utils.error(
                "[LLM_PEDIDO_EXTRACT] Provider response was not a JSON list "
                "response_type=%s response_length=%s input_length=%s",
                type(items_extraidos).__name__,
                len(respuesta_llm_texto),
                len(texto_ocr),
            )
            return []
    except json.JSONDecodeError as exc:
        logger_llm_utils.error(
            "[LLM_PEDIDO_EXTRACT] Invalid JSON response error_type=%s "
            "response_length=%s input_length=%s",
            type(exc).__name__,
            len(respuesta_llm_texto),
            len(texto_ocr),
        )
        return []
    except Exception as exc:
        logger_llm_utils.error(
            "[LLM_PEDIDO_EXTRACT] Response processing failed error_type=%s "
            "response_length=%s input_length=%s",
            type(exc).__name__,
            len(respuesta_llm_texto),
            len(texto_ocr),
        )
        return []

def resumir_descripcion_producto_llm(descripcion_larga: str, max_longitud: int = 200, min_longitud: int = 50) -> str:
    """
    Resume una descripción de producto utilizando un LLM.

    Args:
        descripcion_larga: La descripción original del producto.
        max_longitud: La longitud máxima deseada para el resumen (en caracteres).
        min_longitud: La longitud mínima deseada para el resumen (en caracteres).

    Returns:
        La descripción resumida, o la original si ya es corta o falla el resumen.
    """
    if not descripcion_larga or not isinstance(descripcion_larga, str):
        return ""

    len_original = len(descripcion_larga)

    if len_original <= max_longitud: # Si ya es suficientemente corta
        # Podríamos incluso devolverla si es un poco más larga que min_longitud, para no resumir innecesariamente
        if len_original >= min_longitud or len_original <= max_longitud * 0.75: # No resumir si ya está en un rango aceptable
             return descripcion_larga.strip()


    prompt = (
        f"Eres un experto en marketing. Resume la siguiente descripción de producto para que sea concisa, atractiva y no exceda los {max_longitud} caracteres, "
        f"pero intenta que tenga al menos {min_longitud} caracteres si es posible. "
        "Destaca los beneficios clave o características únicas. Evita jerga innecesaria. "
        "El resultado debe ser solo el texto resumido.\n\n"
        f"Descripción Original:\n\"\"\"\n{descripcion_larga}\n\"\"\"\n\n"
        "Resumen Optimizado:"
    )

    resumen = ""
    try:
        # Usar un modelo eficiente para resúmenes.
        # El modelo por defecto en robust_chat es command-r-plus.
        # Si se necesita un modelo específico aquí, robust_chat debería ser adaptado.
        resumen_candidato = robust_chat(message=prompt) # Removed model_override

        if resumen_candidato:
            resumen = resumen_candidato.strip()
            # Validar longitud del resumen y ajustar si es necesario (simple recorte)
            if len(resumen) > max_longitud:
                # Intentar cortar por la última frase completa dentro del límite
                last_period = resumen.rfind('.', 0, max_longitud)
                if last_period != -1:
                    resumen = resumen[:last_period+1]
                else: # Si no hay punto, cortar bruscamente
                    resumen = resumen[:max_longitud].rsplit(' ', 1)[0] + "..." if ' ' in resumen[:max_longitud] else resumen[:max_longitud]

            if len(resumen) < min_longitud and len_original > min_longitud : # Si el resumen es demasiado corto y el original no
                # Podríamos intentar re-prompting con "hazlo un poco más largo" o simplemente usar el original truncado
                logger.warning(
                    "[LLM_RESUMEN_PROD] Summary below minimum "
                    "summary_length=%s minimum_length=%s original_length=%s",
                    len(resumen),
                    min_longitud,
                    len_original,
                )
                # Fallback a una porción del original si el resumen es insatisfactorio
                return descripcion_larga[:max_longitud].strip()


            logger.info(
                "[LLM_RESUMEN_PROD] Summary completed original_length=%s "
                "summary_length=%s max_length=%s",
                len_original,
                len(resumen),
                max_longitud,
            )
        else:
            logger.warning(
                "[LLM_RESUMEN_PROD] Provider returned empty summary "
                "original_length=%s max_length=%s",
                len_original,
                max_longitud,
            )
            return descripcion_larga[:max_longitud].strip()

    except Exception as exc:
        logger.error(
            "[LLM_RESUMEN_PROD] Summary generation failed error_type=%s "
            "original_length=%s max_length=%s",
            type(exc).__name__,
            len_original,
            max_longitud,
        )
        # Fallback a la descripción original (o una versión truncada si es muy larga)
        return descripcion_larga[:max_longitud].strip()

    return resumen


def analyze_image_with_google_vision_ocr(image_content: bytes) -> str:
    """Extracts text from an image using Google Cloud Vision's OCR capabilities."""
    if not vision:
        logger.error("Google Cloud Vision library not available. Cannot analyze image.")
        return ""

    client = VISION_CLIENT
    if not client:
        try:
            client = vision.ImageAnnotatorClient()
        except Exception as exc:
            logger.error(
                "Failed to initialise Vision client error_type=%s",
                type(exc).__name__,
            )
            return ""

    try:
        image = vision.Image(content=image_content)
        response = client.text_detection(image=image)
        if response.error.message:
            logger.error(
                "Vision API returned an error error_type=provider_error "
                "error_length=%s",
                len(str(response.error.message)),
            )
            return ""
        if response.text_annotations:
            return response.text_annotations[0].description or ""
    except Exception as exc:
        logger.error(
            "Vision OCR failed error_type=%s content_length=%s",
            type(exc).__name__,
            len(image_content or b""),
        )
    return ""

def analyze_document_with_google_document_ai(
    project_id: str,
    location: str,
    processor_id: str,
    file_content: bytes,
    mime_type: str
) -> documentai.Document | None: # Return type includes None for error cases
    """
    Processes a document using Google Cloud Document AI.

    Args:
        project_id: Google Cloud project ID.
        location: Location of the Document AI processor.
        processor_id: ID of the Document AI processor.
        file_content: Bytes of the document file.
        mime_type: Mime type of the document (e.g., "application/pdf", "image/jpeg").

    Returns:
        A Document AI Document object, or None if an error occurs.
    """
    if not documentai or not hasattr(documentai, 'DocumentProcessorServiceClient'): # Check if real or mock
        logger.error("Google Cloud DocumentAI library not available or not fully mocked. Cannot analyze document.")
        return None

    logger.info(
        "Document AI processing requested content_length=%s "
        "has_project=%s has_location=%s has_processor=%s has_mime_type=%s",
        len(file_content or b""),
        bool(project_id),
        bool(location),
        bool(processor_id),
        bool(mime_type),
    )
    # In a real implementation:
    # try:
    #     opts = {"api_endpoint": f"{location}-documentai.googleapis.com"}
    #     client = documentai.DocumentProcessorServiceClient(client_options=opts)
    #     name = client.processor_path(project_id, location, processor_id)
    #     raw_document = documentai.RawDocument(content=file_content, mime_type=mime_type)
    #     request = documentai.ProcessRequest(name=name, raw_document=raw_document)
    #     result = client.process_document(request=request)
    #     return result.document
    # except Exception as e:
    #     logger.error(f"Error in analyze_document_with_google_document_ai: {e}", exc_info=True)
    #     return None

    # Example of returning a mock Document object for placeholder purposes:
    # Ensure the mock object is compatible with what the calling code might expect.

    # Actual Google Document AI client initialization and call
    try:
        # The opts dictionary should be defined using the location variable
        opts = {}
        if location: # Ensure location is not None or empty
            opts["api_endpoint"] = f"{location}-documentai.googleapis.com"

        # Initialize client with or without opts based on whether location was valid
        if opts:
            client = documentai.DocumentProcessorServiceClient(client_options=opts)
        else: # Fallback if location is not set, though this might lead to errors if endpoint isn't default
            logger.warning(
                "Document AI location not set; using default endpoint "
                "has_project=%s has_processor=%s",
                bool(project_id),
                bool(processor_id),
            )
            client = documentai.DocumentProcessorServiceClient()

        name = client.processor_path(project_id, location, processor_id)

        # Construct the RawDocument
        raw_document = documentai.RawDocument(content=file_content, mime_type=mime_type)

        # Construct the request
        request = documentai.ProcessRequest(name=name, raw_document=raw_document)

        logger.info(
            "Processing document with Document AI content_length=%s has_processor_path=%s",
            len(file_content or b""),
            bool(name),
        )
        result = client.process_document(request=request)
        logger.info("Document AI processing complete.")
        return result.document

    except ImportError: # Should have been caught by the check at the top of the function
        logger.error("Google Cloud DocumentAI library not available during client instantiation.")
        return None
    except Exception as exc:
        logger.error(
            "Document AI processing failed error_type=%s content_length=%s",
            type(exc).__name__,
            len(file_content or b""),
        )
        # Return a mock/empty document with error information if possible, or just None
        error_doc_text = f"Error processing document with Document AI: {str(exc)}"
        if isinstance(documentai, type) and hasattr(documentai, 'Document'): # Check if it's the MockDocumentAI class
             # Create a mock document indicating error.
            mock_error_doc = documentai.Document(text=error_doc_text, mime_type=mime_type)
            # You could add custom fields/entities to this mock_error_doc if your calling code checks for them.
            # For example: mock_error_doc.entities = [{'type_': 'error', 'mention_text': str(e)}]
            return mock_error_doc
        # If using the real library and an error occurs, it might raise an exception
        # or return a response with an error field. Here we return None.
        return None
