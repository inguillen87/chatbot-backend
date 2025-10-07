"""Utility to parse municipal agenda text into structured events."""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Dict, List, Union


DAY_PATTERN = re.compile(r"^\*([A-Za-zÁÉÍÓÚáéíóúñÑ]+ \d{1,2})\*")
DAY_NAMES = {"lunes", "martes", "miercoles", "miércoles", "jueves", "viernes", "sabado", "sábado", "domingo"}
TIME_PREFIX = "🕑"
DESC_PREFIX = "✅"
LOC_PREFIX = "📍"

HEADER_KEYWORDS = {"noticia", "noticias", "evento", "eventos", "agenda", "actividades", "informacion", "información", "promocion", "promoción"}
HEADER_EMOJIS = (
    "•",
    "🗞️",
    "📰",
    "🎭",
    "🎟️",
    "🎫",
    "🎉",
    "🎊",
    "🎵",
    "🎶",
    "🎤",
    "🎧",
    "🎷",
    "🎸",
    "🎹",
    "🎼",
    "🎨",
    "🎬",
    "🎪",
    "🏛️",
    "🏟️",
    "🏞️",
)
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")
NOISE_KEYWORDS = {
    "seguinos en nuestras redes",
    "seguinos en nuestras red",
    "ver completo",
    "facebook",
    "instagram",
    "youtube",
    "tiktok",
    "whatsapp",
    "juninmendoza.gov.ar",
}


def parse_agenda_text(text: str) -> List[Dict[str, Union[str, List[str]]]]:
    """Parse agenda text into structured events.

    The parser first attempts to interpret the traditional WhatsApp agenda
    format that uses ``🕑``, ``✅`` and ``📍`` markers. If no events are found
    with that structure it falls back to a more permissive parser that can read
    richer bulletin-style templates (e.g. sections headed with ``•`` and dates
    preceded by ``📅``).
    """

    classic_events = _parse_classic_agenda(text)
    if classic_events:
        return classic_events

    return _parse_bullet_agenda(text)


def _strip_accents(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))


def _extract_day_header(line: str) -> str | None:
    """Return the normalized day header if ``line`` starts with a weekday name."""

    cleaned = line.strip()
    if not cleaned:
        return None

    # Remove decorative characters around the potential day label (asterisks, dashes, etc.)
    cleaned = re.sub(r"^[^A-Za-zÁÉÍÓÚáéíóúñÑ]+", "", cleaned)
    cleaned = re.sub(r"[^A-Za-zÁÉÍÓÚáéíóúñÑ0-9 ]+$", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return None

    first_token = cleaned.split(" ", 1)[0]
    normalized_token = _strip_accents(first_token).casefold()
    if normalized_token not in DAY_NAMES:
        return None

    # Standardize casing for readability (Miércoles 3, Sábado 10, etc.).
    # Preserve any trailing information such as the day number or month name.
    remaining = cleaned[len(first_token):].strip()
    normalized_first = first_token.capitalize()
    if normalized_token in {"miercoles", "miércoles"}:
        normalized_first = "Miércoles"
    elif normalized_token in {"sabado", "sábado"}:
        normalized_first = "Sábado"
    elif normalized_token == "lunes":
        normalized_first = "Lunes"
    elif normalized_token == "martes":
        normalized_first = "Martes"
    elif normalized_token == "jueves":
        normalized_first = "Jueves"
    elif normalized_token == "viernes":
        normalized_first = "Viernes"
    elif normalized_token == "domingo":
        normalized_first = "Domingo"

    return f"{normalized_first}{(' ' + remaining) if remaining else ''}".strip()


def _parse_classic_agenda(text: str) -> List[Dict[str, str]]:
    """Parse the traditional agenda format that uses 🕑/✅/📍 markers."""

    events: List[Dict[str, str]] = []
    current_day: str | None = None
    current_event: Dict[str, str] = {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        day_match = DAY_PATTERN.match(line)
        if day_match:
            extracted_day = day_match.group(1)
        else:
            extracted_day = _extract_day_header(line)

        if extracted_day:
            if current_event:
                events.append(current_event)
                current_event = {}
            current_day = extracted_day
            continue

        if line.startswith(TIME_PREFIX):
            if current_day is None:
                continue  # time without a day header
            if current_event:
                events.append(current_event)
                current_event = {}
            current_event = {"day": current_day, "time": line[len(TIME_PREFIX):].strip()}
            continue

        if line.startswith(DESC_PREFIX) and current_event:
            current_event["title"] = line[len(DESC_PREFIX):].strip()
            continue

        if line.startswith(LOC_PREFIX) and current_event:
            current_event["location"] = line[len(LOC_PREFIX):].strip()
            events.append(current_event)
            current_event = {}
            continue

    if current_event:
        events.append(current_event)

    return events


def _parse_bullet_agenda(text: str) -> List[Dict[str, Union[str, List[str]]]]:
    """Parse agenda bulletins that rely on 📅 markers and rich sections."""

    lines = [line.strip() for line in text.splitlines()]
    if not any(line.startswith("📅") for line in lines):
        return []

    header_indexes: set[int] = set()
    header_for_index: dict[int, str | None] = {}
    current_header: str | None = None

    for idx, line in enumerate(lines):
        if not line:
            header_for_index[idx] = current_header
            continue
        if _is_header_line(line):
            current_header = _clean_header(line)
            header_indexes.add(idx)
            header_for_index[idx] = current_header
            continue
        header_for_index[idx] = current_header

    events: List[Dict[str, Union[str, List[str]]]] = []
    total_lines = len(lines)

    for idx, line in enumerate(lines):
        if not line or not line.startswith("📅"):
            continue

        event: Dict[str, Union[str, List[str]]] = {}
        time_text = line[len("📅"):].strip()
        if time_text:
            event["time"] = time_text

        header_value = header_for_index.get(idx)
        if header_value:
            event["tags"] = [header_value]
            tipo_post = _infer_tipo_from_header(header_value)
            if tipo_post:
                event["tipo_post"] = tipo_post

        before_lines: List[str] = []
        j = idx - 1
        while j >= 0:
            prev = lines[j]
            if not prev:
                if before_lines:
                    break
                j -= 1
                continue
            if j in header_indexes or prev.startswith("📅"):
                break
            if _is_noise_line(prev):
                j -= 1
                continue
            if _looks_like_image_line(prev) and "imagen_url" not in event:
                event["imagen_url"] = prev
                j -= 1
                continue
            if prev.startswith("🔗"):
                event.setdefault("enlace", prev[len("🔗"):].strip())
                j -= 1
                continue
            if prev.startswith("📍"):
                event.setdefault("location", prev[len("📍"):].strip())
                j -= 1
                continue
            before_lines.append(prev)
            j -= 1

        before_lines.reverse()
        if before_lines:
            event["title"] = before_lines[0]
            if len(before_lines) > 1:
                description = "\n".join(before_lines[1:]).strip()
                if description:
                    event["description"] = description

        after_lines: List[str] = []
        j = idx + 1
        while j < total_lines:
            nxt = lines[j]
            if not nxt:
                if after_lines:
                    k = j + 1
                    while k < total_lines and not lines[k]:
                        k += 1
                    if k >= total_lines:
                        j = k
                        break
                    if k in header_indexes or lines[k].startswith("📅") or _is_header_line(lines[k]):
                        j = k
                        break
                    j = k
                    continue
                j += 1
                continue
            if j in header_indexes or nxt.startswith("📅"):
                break
            if _is_noise_line(nxt):
                j += 1
                continue
            if _looks_like_image_line(nxt) and "imagen_url" not in event:
                event["imagen_url"] = nxt
                j += 1
                continue
            if nxt.startswith("📍"):
                event["location"] = nxt[len("📍"):].strip()
                j += 1
                continue
            if nxt.startswith("🔗"):
                event["enlace"] = nxt[len("🔗"):].strip()
                j += 1
                continue
            after_lines.append(nxt)
            j += 1

        if after_lines:
            extra_desc = "\n".join(after_lines).strip()
            if extra_desc:
                if "description" in event:
                    event["description"] = f"{event['description']}\n{extra_desc}".strip()
                else:
                    event["description"] = extra_desc

        if not event.get("title"):
            if event.get("description"):
                event["title"] = event["description"].splitlines()[0]
            elif header_value:
                event["title"] = header_value

        if event.get("title"):
            events.append(event)

    return events


def _looks_like_image_line(line: str) -> bool:
    lowered = line.casefold()
    return any(lowered.endswith(ext) for ext in IMAGE_EXTENSIONS)


def _clean_header(line: str) -> str:
    cleaned = re.sub(r"^[\s•\-]+", "", line).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def _is_header_line(line: str) -> bool:
    if not line:
        return False
    normalized = line.strip()
    if not normalized:
        return False
    if normalized.startswith(HEADER_EMOJIS):
        return True
    stripped = re.sub(r"^[\W_]+", "", normalized)
    lowered = stripped.casefold()
    return any(keyword in lowered for keyword in HEADER_KEYWORDS)


def _is_noise_line(line: str) -> bool:
    if not line:
        return True
    lowered = line.casefold()
    if any(keyword in lowered for keyword in NOISE_KEYWORDS):
        return True
    if re.fullmatch(r"\d{1,2}:\d{2}", line):
        return True
    if re.fullmatch(r"\d+", line):
        return True
    return False


def _infer_tipo_from_header(header: str) -> str | None:
    lowered = header.casefold()
    if "noticia" in lowered:
        return "noticia"
    if "evento" in lowered or "agenda" in lowered or "actividades" in lowered:
        return "evento"
    if "informacion" in lowered or "información" in lowered:
        return "informacion"
    if "promocion" in lowered or "promoción" in lowered or "promo" in lowered:
        return "promocion"
    return None


def parse_agenda_file(path: Union[str, Path]) -> List[Dict[str, Union[str, List[str]]]]:
    """Parse an agenda text file (.txt or .docx) into structured events."""

    file_path = Path(path)
    if file_path.suffix.lower() == ".docx":
        try:
            from docx import Document  # type: ignore
        except ImportError as exc:  # pragma: no cover - handled in tests
            raise ImportError(
                "python-docx is required to parse .docx files"
            ) from exc
        document = Document(file_path)
        text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    else:
        text = file_path.read_text(encoding="utf-8")

    return parse_agenda_text(text)
