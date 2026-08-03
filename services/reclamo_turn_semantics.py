"""State-specific semantics for the guided municipal claim flow.

This module deliberately does *not* try to classify every possible municipal
intent.  It only resolves the small, safety-critical set of answers accepted
while the user is looking at the photo or confirmation step.  Keeping these
guards deterministic prevents a conversational sentence such as ``Si, quiero
corregir la direccion`` from being reduced to the affirmative token ``si`` and
creating a claim with stale data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
import unicodedata
from typing import Any, Mapping


class ReclamoTurnIntent(str, Enum):
    """Semantic actions understood by the guided claim flow."""

    CONFIRM = "confirm"
    EDIT = "edit"
    CANCEL = "cancel"
    NEW_CLAIM = "new_claim"
    ADD_PHOTO = "add_photo"
    SKIP_PHOTO = "skip_photo"
    CORRECTION = "correction"
    FOLLOW_UP_QUESTION = "follow_up_question"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ReclamoTurnDecision:
    """A classified turn plus any safe structured mutations it carries."""

    intent: ReclamoTurnIntent
    corrections: Mapping[str, str] = field(default_factory=dict)
    clear_fields: tuple[str, ...] = ()
    reason: str = ""


_ACTION_INTENTS = {
    "reclamo_confirmar_si": ReclamoTurnIntent.CONFIRM,
    "reclamo_confirmar_no": ReclamoTurnIntent.EDIT,
    "reclamo_cancelar": ReclamoTurnIntent.CANCEL,
    "cancelar": ReclamoTurnIntent.CANCEL,
    "menu_principal": ReclamoTurnIntent.CANCEL,
    "reclamo_adjuntar_foto_si": ReclamoTurnIntent.ADD_PHOTO,
    "reclamo_adjuntar_foto_no": ReclamoTurnIntent.SKIP_PHOTO,
    "mostrar_menu_reclamos": ReclamoTurnIntent.NEW_CLAIM,
    "iniciar_reclamo": ReclamoTurnIntent.NEW_CLAIM,
    "crear_reclamo": ReclamoTurnIntent.NEW_CLAIM,
}

_CONFIRMATION_EMOJI_INTENTS = {
    "✅": ReclamoTurnIntent.CONFIRM,
    "👍": ReclamoTurnIntent.CONFIRM,
    "✏": ReclamoTurnIntent.EDIT,
    "❌": ReclamoTurnIntent.CANCEL,
    "📷": ReclamoTurnIntent.ADD_PHOTO,
}

_PHOTO_EMOJI_INTENTS = {
    "📷": ReclamoTurnIntent.ADD_PHOTO,
    "🚫": ReclamoTurnIntent.SKIP_PHOTO,
    "⏭": ReclamoTurnIntent.SKIP_PHOTO,
    "❌": ReclamoTurnIntent.CANCEL,
}

_PURE_CONFIRMATIONS = {
    "1",
    "acepto",
    "confirmado",
    "confirmar",
    "confirmo",
    "correcto",
    "dale",
    "dale confirmo",
    "de acuerdo",
    "esta bien",
    "esta todo bien",
    "listo confirmo",
    "los datos estan bien",
    "ok",
    "okay",
    "perfecto",
    "si",
    "si confirmo",
    "si esta bien",
    "si los datos estan bien",
    "todo correcto",
}

_PHOTO_SKIP_ANSWERS = {
    "2",
    "asi esta bien",
    "continuar sin foto",
    "dejalo asi",
    "eso es todo",
    "esta bien asi",
    "listo",
    "nada mas",
    "nada mas gracias",
    "ninguna",
    "no",
    "no hace falta",
    "no nada mas",
    "no necesito agregar nada",
    "no quiero agregar foto",
    "no quiero agregar una foto",
    "no tengo foto",
    "no tengo una foto",
    "omitir",
    "omitir foto",
    "segui sin foto",
    "seguir sin foto",
    "sin foto",
}

_PHOTO_ADD_ANSWERS = {
    "1",
    "adjuntar foto",
    "agregar foto",
    "enviar foto",
    "mandar foto",
    "si",
    "si agregar foto",
    "si quiero agregar foto",
    "si quiero agregar una foto",
}

_PHOTO_SKIP_PATTERNS = (
    r"^(?:no\s+)?nada\s+mas(?:\s+gracias)?$",
    r"\bno\s+hace\s+falta(?:\s+(?:una|la))?\s+(?:foto|imagen)\b",
    r"\b(?:continuar|continuamos|segui|seguir|seguimos)\s+sin\s+(?:foto|imagen)\b",
    r"\bno\s+(?:puedo|quiero|tengo)\b.{0,25}\b(?:foto|imagen)\b",
)

_GENERIC_EDIT_ANSWERS = {
    "2",
    "cambiar",
    "corregir",
    "editar",
    "editar datos",
    "modificar",
    "no",
    "no estan bien",
    "quiero corregir",
    "quiero editar",
    "quiero modificar",
}

_CANCEL_ANSWERS = {
    "3",
    "cancelar",
    "cancela el reclamo",
    "menu",
    "menu principal",
    "salir",
    "terminar",
}

_NEW_CLAIM_PHRASES = (
    "crear un reclamo",
    "hacer otro reclamo",
    "hacer un reclamo",
    "iniciar otro reclamo",
    "iniciar un reclamo",
    "nuevo reclamo",
    "otro reclamo",
)

_ADD_PHOTO_PATTERNS = (
    r"\b(?:adjunt|agreg|envi|mand|sub)[a-z]*\b.{0,30}\b(?:foto|imagen)\b",
    r"\b(?:foto|imagen)\b.{0,30}\b(?:adjunt|agreg|envi|mand|sub)[a-z]*\b",
    r"\b(?:te\s+)?(?:mando|envio)\s+(?:una\s+)?(?:foto|imagen)\b",
    r"\bquiero\s+(?:poner|sumar)\s+(?:una\s+)?(?:foto|imagen)\b",
)

_FOLLOW_UP_PATTERNS = (
    r"\b(?:cual|dame|decime|donde|como|necesito|recordame)\b.{0,45}\bpin\b",
    r"\b(?:mi|el)\s+pin\b",
    r"\bpin\b.{0,55}\b(?:consult|pregunt|seguimiento|futuro|ticket|reclamo)\w*\b",
    r"\b(?:numero|codigo|link|enlace)\b.{0,35}\b(?:seguimiento|ticket|reclamo)\b",
    r"\b(?:como|donde)\b.{0,50}\b(?:consult|segu|ver)\w*\b.{0,30}\b(?:ticket|reclamo)\b",
)

_EDIT_REQUEST_PATTERNS = (
    r"\b(?:quiero|necesito|deseo)?\s*(?:cambiar|corregir|editar|modificar|actualizar)\b.{0,45}\b(?:datos|direccion|ubicacion|categoria|descripcion|nombre|dni|email|correo|telefono)\b",
    r"\b(?:datos|direccion|ubicacion|categoria|descripcion|nombre|dni|email|correo|telefono)\b.{0,35}\b(?:esta mal|estan mal|no es correcto|no son correctos)\b",
)

_FIELD_ALIASES = {
    "direccion": r"direcci[oó]n|ubicaci[oó]n|domicilio|lugar",
    "categoria": r"categor[ií]a|tipo de reclamo",
    "descripcion": r"descripci[oó]n|detalle(?:s)?|problema",
}
_EMAIL_TOKEN_RE = re.compile(r"(?<!\S)[^@\s]+@[^@\s]+")


def normalize_reclamo_turn(value: Any) -> str:
    """Normalize a user turn for exact, accent-insensitive comparisons."""

    if value is None:
        return ""
    text = str(value).lower().strip()
    text = "".join(
        char
        for char in unicodedata.normalize("NFD", text)
        if not unicodedata.combining(char)
    )
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalized_action(action: Any) -> str:
    return normalize_reclamo_turn(action).replace(" ", "_")


def _isolated_emoji(value: Any) -> str:
    """Return an emoji only when the entire turn consists of that symbol."""

    return re.sub(r"[\s\ufe0e\ufe0f]", "", str(value or ""))


def _extract_field_value(text: str, aliases: str) -> str | None:
    """Extract an explicitly labelled value without guessing unlabeled text."""

    field = rf"(?:{aliases})"
    all_fields = "|".join(rf"(?:{value})" for value in _FIELD_ALIASES.values())
    next_field = re.compile(
        rf",\s*(?=(?:la\s+|el\s+)?(?:{all_fields})\s*"
        rf"(?:(?:correct[ao]\s+)?(?:es|era|queda|seria)\b|[:\-]))",
        flags=re.IGNORECASE,
    )
    patterns = (
        # "La direccion es/era Don Bosco 55..."
        rf"\b(?:la\s+|el\s+)?{field}\s+(?:correct[ao]\s+)?(?:(?:es|era|queda|seria)\b|est[aá]\s+en\b)\s*[:\-]?\s*(?P<value>[^.!?;\n]+)",
        # "Cambiar/corregir la direccion a Don Bosco 55..."
        rf"\b(?:cambi|correg|modific|edit|actualiz|reemplaz|pon)[a-záéíóúñ]*\b(?:\s+\w+){{0,4}}?\s+{field}(?:\s+(?:a|por|como))?\s*[:\-]?\s*(?P<value>[^.!?;\n]+)",
        # "Direccion: Don Bosco 55..." or "Direccion Don Bosco 55..."
        rf"(?:^|[.!?;,\n]\s*){field}\s*[:\-]?\s*(?P<value>[^.!?;\n]+)",
    )

    candidates: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            value = match.group("value").strip(" ,:-\t\r\n")
            value = next_field.split(value, maxsplit=1)[0].strip(" ,:-\t\r\n")
            # A bare editing verb is a request for a prompt, not a value.
            if not value or normalize_reclamo_turn(value) in {
                "a",
                "cambiar",
                "corregir",
                "editar",
                "modificar",
            }:
                continue
            candidates.append(value)

    return candidates[-1] if candidates else None


def extract_reclamo_corrections(user_input: Any) -> dict[str, str]:
    """Return only corrections whose target field is explicit in the turn.

    The extraction is intentionally conservative.  Free text without a field
    label remains an edit request and can be clarified by the conversational
    layer instead of silently overwriting claim data.
    """

    text = str(user_input or "").strip()
    if not text:
        return {}

    # Contact edits can legitimately contain aliases such as "ubicacion" in
    # an email address. Remove complete email tokens before looking for
    # labelled claim fields so a contact update cannot become a bogus address
    # correction.
    text = _EMAIL_TOKEN_RE.sub(" ", text)

    corrections: dict[str, str] = {}
    for field_name, aliases in _FIELD_ALIASES.items():
        value = _extract_field_value(text, aliases)
        if value:
            corrections[field_name] = value
    return corrections


def classify_reclamo_photo_turn(
    user_input: Any,
    *,
    action: Any = None,
    has_photo: bool = False,
) -> ReclamoTurnDecision:
    """Classify a response to the optional-photo prompt."""

    if has_photo:
        return ReclamoTurnDecision(
            ReclamoTurnIntent.ADD_PHOTO,
            reason="photo_payload",
        )

    action_intent = _ACTION_INTENTS.get(_normalized_action(action))
    if action_intent in {
        ReclamoTurnIntent.ADD_PHOTO,
        ReclamoTurnIntent.SKIP_PHOTO,
        ReclamoTurnIntent.CANCEL,
    }:
        return ReclamoTurnDecision(action_intent, reason="explicit_action")

    emoji_intent = _PHOTO_EMOJI_INTENTS.get(_isolated_emoji(user_input))
    if emoji_intent is not None:
        return ReclamoTurnDecision(emoji_intent, reason="isolated_emoji")

    normalized = normalize_reclamo_turn(user_input)
    if normalized in _PHOTO_SKIP_ANSWERS or any(
        re.search(pattern, normalized) for pattern in _PHOTO_SKIP_PATTERNS
    ):
        return ReclamoTurnDecision(
            ReclamoTurnIntent.SKIP_PHOTO,
            reason="explicit_skip_phrase",
        )
    if normalized in _PHOTO_ADD_ANSWERS or any(
        re.search(pattern, normalized) for pattern in _ADD_PHOTO_PATTERNS
    ):
        return ReclamoTurnDecision(
            ReclamoTurnIntent.ADD_PHOTO,
            reason="explicit_add_photo_phrase",
        )
    return ReclamoTurnDecision(ReclamoTurnIntent.UNKNOWN, reason="ambiguous_photo_answer")


def classify_reclamo_confirmation_turn(
    user_input: Any,
    *,
    action: Any = None,
    has_photo: bool = False,
) -> ReclamoTurnDecision:
    """Classify a confirmation turn without token-based false positives.

    Precedence matters: media, corrections, photo requests and questions are
    resolved before pure confirmations.  An explicit UI action remains
    authoritative because it is generated from the current confirmation card.
    """

    if has_photo:
        return ReclamoTurnDecision(ReclamoTurnIntent.ADD_PHOTO, reason="photo_payload")

    action_intent = _ACTION_INTENTS.get(_normalized_action(action))
    if action_intent is not None:
        return ReclamoTurnDecision(action_intent, reason="explicit_action")

    # Some legacy WhatsApp adapters passed the action id as ``Body`` instead
    # of the structured action field.  Accept only an exact known identifier;
    # ordinary prose must still go through the stricter semantic checks below.
    legacy_action_intent = _ACTION_INTENTS.get(_normalized_action(user_input))
    if legacy_action_intent is not None and str(user_input or "").strip().lower().startswith(
        "reclamo_"
    ):
        return ReclamoTurnDecision(legacy_action_intent, reason="legacy_action_body")

    emoji_intent = _CONFIRMATION_EMOJI_INTENTS.get(_isolated_emoji(user_input))
    if emoji_intent is not None:
        return ReclamoTurnDecision(emoji_intent, reason="isolated_emoji")

    normalized = normalize_reclamo_turn(user_input)
    corrections = extract_reclamo_corrections(user_input)
    if corrections:
        clear_fields: tuple[str, ...] = ()
        if "direccion" in corrections:
            clear_fields = (
                "coordenadas",
                "map_search_url",
                "maps_link",
                "static_map_url",
            )
        return ReclamoTurnDecision(
            ReclamoTurnIntent.CORRECTION,
            corrections=corrections,
            clear_fields=clear_fields,
            reason="explicit_field_correction",
        )

    if any(re.search(pattern, normalized) for pattern in _ADD_PHOTO_PATTERNS):
        return ReclamoTurnDecision(
            ReclamoTurnIntent.ADD_PHOTO,
            reason="explicit_add_photo_phrase",
        )

    if any(re.search(pattern, normalized) for pattern in _FOLLOW_UP_PATTERNS):
        return ReclamoTurnDecision(
            ReclamoTurnIntent.FOLLOW_UP_QUESTION,
            reason="tracking_or_pin_question",
        )

    if normalized in _CANCEL_ANSWERS:
        return ReclamoTurnDecision(ReclamoTurnIntent.CANCEL, reason="explicit_cancel")
    if any(phrase in normalized for phrase in _NEW_CLAIM_PHRASES):
        return ReclamoTurnDecision(ReclamoTurnIntent.NEW_CLAIM, reason="new_claim_request")
    if normalized in _GENERIC_EDIT_ANSWERS:
        return ReclamoTurnDecision(ReclamoTurnIntent.EDIT, reason="explicit_edit")
    if any(re.search(pattern, normalized) for pattern in _EDIT_REQUEST_PATTERNS):
        return ReclamoTurnDecision(ReclamoTurnIntent.EDIT, reason="explicit_edit_request")
    if normalized in _PURE_CONFIRMATIONS:
        return ReclamoTurnDecision(ReclamoTurnIntent.CONFIRM, reason="pure_confirmation")

    return ReclamoTurnDecision(
        ReclamoTurnIntent.UNKNOWN,
        reason="ambiguous_confirmation_answer",
    )


def apply_reclamo_corrections(
    claim_data: dict[str, Any],
    decision: ReclamoTurnDecision,
) -> dict[str, Any]:
    """Apply a validated correction decision to an existing draft in place."""

    if decision.intent is not ReclamoTurnIntent.CORRECTION:
        raise ValueError("reclamo_correction_decision_required")
    for field_name, value in decision.corrections.items():
        claim_data[field_name] = value
    for field_name in decision.clear_fields:
        claim_data.pop(field_name, None)
    return claim_data
