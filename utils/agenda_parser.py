"""Utility to parse municipal agenda text into structured events."""
from __future__ import annotations

import re
from pathlib import Path
from typing import List, Dict, Union


DAY_PATTERN = re.compile(r"^\*([A-Za-zÁÉÍÓÚáéíóúñÑ]+ \d{1,2})\*")
TIME_PREFIX = "🕑"
DESC_PREFIX = "✅"
LOC_PREFIX = "📍"


def parse_agenda_text(text: str) -> List[Dict[str, str]]:
    """Parse a WhatsApp-style agenda into structured event dictionaries.

    Parameters
    ----------
    text: str
        Raw agenda text where days are denoted by `*Dia N*` lines and events
        consist of three lines: time, description, and location prefixed by
        specific emojis.

    Returns
    -------
    List[Dict[str, str]]
        A list of events with keys: ``day``, ``time``, ``title``, ``location``.
    """
    events: List[Dict[str, str]] = []
    current_day: str | None = None
    current_event: Dict[str, str] = {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        day_match = DAY_PATTERN.match(line)
        if day_match:
            current_day = day_match.group(1)
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


def parse_agenda_file(path: Union[str, Path]) -> List[Dict[str, str]]:
    """Parse an agenda text file (.txt or .docx) into structured events.

    Parameters
    ----------
    path: Union[str, Path]
        Path to the file containing the agenda text. ``.docx`` files are read
        via ``python-docx`` while other extensions are treated as UTF-8 text.

    Returns
    -------
    List[Dict[str, str]]
        Parsed events in the same format as :func:`parse_agenda_text`.
    """

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
