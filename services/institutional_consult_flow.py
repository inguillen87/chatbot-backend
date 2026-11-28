"""WhatsApp Flow «Encontrá tu trámite» for any published institutional assistant.

TDF's pilot form was written by hand. This builds the same experience from a
tenant's published knowledge pack, so any municipality or agency gets it:

* one screen, no endpoint: the person picks a topic (and a city when the pack
  publishes localities) and the form completes with short ids only;
* titles come from the published pack and are trimmed to Meta's limits;
* the completion payload is ``{"tema": <topic id>, "ciudad": <locality slug>?}``,
  the shape the WhatsApp side already maps to a knowledge node.

Nothing here talks to Meta; the result is a validated Flow JSON artifact.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Mapping, Sequence

from services.meta_flow_json import FlowBlueprint, FlowJsonArtifact, compile_flow_blueprint

MAX_OPTIONS = 20
TITLE_MAX = 30
SCREEN_ID = "CONSULTA"


def slug(value: Any) -> str:
    text = unicodedata.normalize("NFD", str(value or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:60]


def _title(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= TITLE_MAX else text[: TITLE_MAX - 1].rstrip() + "…"


def consult_options(knowledge: Mapping[str, Any]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Topics and localities a person can choose, from a published pack."""
    topics: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in knowledge.get("topics") or []:
        if not isinstance(row, Mapping):
            continue
        topic_id = str(row.get("id") or "").strip()
        label = _title(row.get("label"))
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", topic_id) or not label or topic_id in seen:
            continue
        seen.add(topic_id)
        topics.append({"id": topic_id, "title": label})
    localities: list[dict[str, str]] = []
    for row in knowledge.get("localities") or []:
        if not isinstance(row, Mapping):
            continue
        locality_id, label = slug(row.get("label")), _title(row.get("label"))
        if locality_id and label and all(item["id"] != locality_id for item in localities):
            localities.append({"id": locality_id, "title": label})
    return topics[:MAX_OPTIONS], localities[:MAX_OPTIONS]


def build_consult_blueprint(*, organization: str, knowledge: Mapping[str, Any]) -> FlowBlueprint:
    topics, localities = consult_options(knowledge)
    if not topics:
        raise ValueError("knowledge_pack_without_topics")
    children: list[dict[str, Any]] = [
        {"type": "TextHeading", "text": "Encontrá tu trámite"},
        {"type": "TextBody", "text": "Elegí el tema y te mostramos la información oficial en este chat."
         if not localities else "Elegí tu ciudad y el tema. Te mostramos la información oficial en este chat."},
    ]
    payload: dict[str, str] = {"tema": "${form.tema}"}
    if localities:
        children.append({"type": "RadioButtonsGroup", "name": "ciudad", "label": "¿Desde qué ciudad consultás?",
                         "required": True, "data-source": localities})
        payload = {"ciudad": "${form.ciudad}", **payload}
    children.append({"type": "RadioButtonsGroup", "name": "tema", "label": "¿Sobre qué tema?",
                     "required": True, "data-source": topics})
    children.append({"type": "TextCaption", "text": "Información pública. No hace falta compartir datos personales."})
    children.append({"type": "Footer", "label": "Ver la información",
                     "on-click-action": {"name": "complete", "payload": payload}})
    return FlowBlueprint(
        name=f"consulta_{slug(organization) or 'organizacion'}",
        endpoint_driven=False,
        screens=({
            "id": SCREEN_ID,
            "title": _title(organization) or "Consulta",
            "terminal": True,
            "success": True,
            "data": {},
            "layout": {"type": "SingleColumnLayout", "children": children},
        },),
    )


def build_consult_flow(*, organization: str, knowledge: Mapping[str, Any]) -> FlowJsonArtifact:
    return compile_flow_blueprint(build_consult_blueprint(organization=organization, knowledge=knowledge))


def consult_flow_summary(topics: Sequence[Mapping[str, str]], localities: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    return {"topics": list(topics), "localities": list(localities),
            "completion_payload": ["ciudad", "tema"] if localities else ["tema"]}
