"""Utilities to load reusable NLP vocabularies from external resources."""
from __future__ import annotations

import json
import logging
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Mapping

from services.spacy_loader import get_spacy_model

logger = logging.getLogger(__name__)

_VOCAB_DIR = Path(__file__).resolve().parent.parent / "data" / "vocabulary"
_VOCAB_FILE = _VOCAB_DIR / "ticket_vocabulary.json"


def _normalize(text: str) -> str:
    """Return a lowercase token without diacritics for consistent matching."""
    if not text:
        return ""
    normalized = unicodedata.normalize("NFD", text)
    stripped = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    return stripped.lower()


def _normalize_set(values: Iterable[str]) -> frozenset[str]:
    return frozenset(filter(None, (_normalize(value) for value in values)))


def _normalize_list(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(filter(None, (_normalize(value) for value in values)))


def _normalize_with_originals(values: Iterable[str]) -> frozenset[str]:
    tokens: set[str] = set()
    for value in values:
        if not value:
            continue
        lowered = value.strip().lower()
        if lowered:
            tokens.add(lowered)
            tokens.add(_normalize(lowered))
    return frozenset(tokens)


def _load_vocab_resource(path: Path) -> Mapping[str, object]:
    if not path.exists():
        raise FileNotFoundError(f"Vocabulary file not found: {path}")
    with path.open("r", encoding="utf-8") as fp:
        return json.load(fp)


def _load_spacy_stopwords() -> set[str]:
    spacy_stopwords: set[str] = set()
    nlp = get_spacy_model()
    if not nlp:
        logger.warning("spaCy model not available. Using only custom stopwords.")
        return spacy_stopwords
    for word in nlp.Defaults.stop_words:
        norm = _normalize(word)
        if norm:
            spacy_stopwords.add(norm)
    return spacy_stopwords


@dataclass(frozen=True)
class TicketVocabulary:
    stopwords: frozenset[str]
    name_prefix_stopwords: frozenset[str]
    priority_issues: frozenset[str]
    priority_locations: frozenset[str]
    location_priority_order: tuple[str, ...]
    location_tokens: frozenset[str]
    descriptive_tokens: frozenset[str]
    impact_tokens: frozenset[str]
    impact_keywords: frozenset[str]
    impact_substrings: tuple[str, ...]
    filler_patterns: tuple[str, ...]
    verb_endings: tuple[str, ...]
    verb_exceptions: frozenset[str]


@lru_cache(maxsize=1)
def get_ticket_vocabulary() -> TicketVocabulary:
    """Load reusable keyword sets for complaint summarisation flows."""
    raw_data = _load_vocab_resource(_VOCAB_FILE)

    extra_stopwords = raw_data.get("extra_stopwords", [])
    name_prefix_stopwords = raw_data.get("name_prefix_stopwords", [])

    stopwords = set(_normalize_set(extra_stopwords))
    stopwords.update(_load_spacy_stopwords())

    priority_locations = _normalize_set(raw_data.get("priority_locations", []))
    location_tokens_extra = _normalize_set(raw_data.get("location_tokens_extra", []))

    return TicketVocabulary(
        stopwords=frozenset(stopwords),
        name_prefix_stopwords=_normalize_with_originals(name_prefix_stopwords),
        priority_issues=_normalize_set(raw_data.get("priority_issues", [])),
        priority_locations=priority_locations,
        location_priority_order=_normalize_list(raw_data.get("location_priority_order", [])),
        location_tokens=frozenset(priority_locations | location_tokens_extra),
        descriptive_tokens=_normalize_set(raw_data.get("descriptive_tokens", [])),
        impact_tokens=_normalize_set(raw_data.get("impact_tokens", [])),
        impact_keywords=_normalize_set(raw_data.get("impact_keywords", [])),
        impact_substrings=_normalize_list(raw_data.get("impact_substrings", [])),
        filler_patterns=tuple(raw_data.get("filler_patterns", [])),
        verb_endings=tuple(raw_data.get("verb_endings", [])),
        verb_exceptions=_normalize_set(raw_data.get("verb_exceptions", [])),
    )


def get_stopwords() -> frozenset[str]:
    return get_ticket_vocabulary().stopwords


@lru_cache(maxsize=1)
def get_name_prefix_stopwords() -> frozenset[str]:
    """Load name-prefix words without initializing the optional NLP model."""
    raw_data = _load_vocab_resource(_VOCAB_FILE)
    return _normalize_with_originals(raw_data.get("name_prefix_stopwords", []))
