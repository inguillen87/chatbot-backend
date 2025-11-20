from __future__ import annotations

import logging
from typing import List, Optional

import openai

logger = logging.getLogger(__name__)


def _client() -> object:
    ctor = getattr(openai, "OpenAI", None)
    if ctor:
        return ctor()
    return openai


def extract_table_from_file(file_bytes: bytes, prompt: str, model: str = "gpt-4.1-mini") -> Optional[List[dict]]:
    try:
        client = _client()
        response = client.responses.create(  # type: ignore[attr-defined]
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "input_file", "input_file": file_bytes},
                    ],
                }
            ],
            format={"type": "json_object"},
        )
        message = response.output[0].content[0].text  # type: ignore[index]
        import json

        data = json.loads(message)
        rows = data.get("items") or data.get("productos") or data
        if isinstance(rows, list):
            return rows
    except Exception as exc:  # pragma: no cover - best effort
        logger.error("Error usando OpenAI Vision: %s", exc)
    return None

