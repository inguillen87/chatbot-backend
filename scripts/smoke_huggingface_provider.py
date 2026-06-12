from __future__ import annotations

import argparse
import json
import os
from typing import Any


DEFAULT_TEXT = "Hay una luminaria rota en la esquina y la zona queda oscura."
DEFAULT_LABELS = ["Luminaria", "Arbolado", "Limpieza y riego", "Arreglo de calle", "Perdida de agua", "Otros"]


def smoke_huggingface_zero_shot(text: str = DEFAULT_TEXT, labels: list[str] | None = None) -> dict[str, Any]:
    token = (os.getenv("HUGGINGFACE_API_TOKEN") or os.getenv("HF_TOKEN") or "").strip()
    if not token:
        return {
            "ok": False,
            "reason_code": "huggingface_token_missing",
            "next_action": "set HUGGINGFACE_API_TOKEN in the process environment",
            "secret_values_printed": False,
        }

    os.environ.setdefault("HUGGINGFACE_ZERO_SHOT_ENABLED", "true")

    from services import huggingface_inference_service as hf

    resolved_labels = labels or DEFAULT_LABELS
    results = hf.classify_zero_shot(text, resolved_labels, multi_label=False)
    if not results:
        return {
            "ok": False,
            "reason_code": "huggingface_zero_shot_empty_result",
            "model": os.getenv("HUGGINGFACE_ZERO_SHOT_MODEL", "joeddav/xlm-roberta-large-xnli"),
            "secret_values_printed": False,
        }

    top = results[0]
    return {
        "ok": True,
        "model": os.getenv("HUGGINGFACE_ZERO_SHOT_MODEL", "joeddav/xlm-roberta-large-xnli"),
        "top_label": top.get("label"),
        "top_score": top.get("score"),
        "candidate_count": len(results),
        "secret_values_printed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test Hugging Face zero-shot classification without printing tokens.")
    parser.add_argument("--text", default=DEFAULT_TEXT, help="Text to classify.")
    parser.add_argument(
        "--labels",
        default=",".join(DEFAULT_LABELS),
        help="Comma-separated candidate labels.",
    )
    args = parser.parse_args()

    labels = [label.strip() for label in args.labels.split(",") if label.strip()]
    result = smoke_huggingface_zero_shot(args.text, labels)
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
