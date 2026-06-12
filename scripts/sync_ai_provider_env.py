from __future__ import annotations

import argparse
import json
import os
from collections import OrderedDict
from typing import Any

from services import render_env_sync


SECRET_KEYS = ("GEMINI_API_KEY", "HUGGINGFACE_API_TOKEN")

RECOMMENDED_DEFAULTS = OrderedDict(
    [
        ("LLM_PROVIDER_ORDER", "openai,gemini"),
        ("GEMINI_CHAT_MODEL", "gemini-2.5-flash"),
        ("HUGGINGFACE_ENABLED", "true"),
        ("HUGGINGFACE_PROVIDER", "auto"),
        ("HUGGINGFACE_ZERO_SHOT_ENABLED", "true"),
        ("HUGGINGFACE_ZERO_SHOT_MODEL", "joeddav/xlm-roberta-large-xnli"),
        ("HUGGINGFACE_RECLAMO_CATEGORY_MIN_SCORE", "0.72"),
        ("HUGGINGFACE_RECLAMO_PRIORITY_MIN_SCORE", "0.66"),
        ("HUGGINGFACE_EMBEDDINGS_ENABLED", "false"),
        ("HUGGINGFACE_EMBEDDING_MODEL", "intfloat/multilingual-e5-large"),
        ("VISION_HUGGINGFACE_ENABLED", "false"),
        ("HUGGINGFACE_IMAGE_CLASSIFICATION_MODEL", "google/vit-base-patch16-224"),
        ("HUGGINGFACE_OBJECT_DETECTION_MODEL", "facebook/detr-resnet-50"),
        ("INSTALL_OPEN_SOURCE_AI_EXTRAS", "false"),
        ("DOCLING_ENABLED", "false"),
        ("DOCLING_MAX_FILE_MB", "15"),
    ]
)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _secret_values(values: dict[str, str]) -> list[str]:
    return [value for key, value in values.items() if key in SECRET_KEYS and value]


def _redact(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, dict):
        return {key: _redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, str):
        redacted = value
        for secret in secrets:
            if secret:
                redacted = redacted.replace(secret, "[redacted]")
        return redacted
    return value


def collect_ai_env_values(*, include_recommended_defaults: bool = False) -> dict[str, str]:
    values: dict[str, str] = {}

    for key in SECRET_KEYS:
        value = _clean(os.getenv(key))
        if value:
            values[key] = value

    for key, default in RECOMMENDED_DEFAULTS.items():
        value = _clean(os.getenv(key))
        if value:
            values[key] = value
        elif include_recommended_defaults:
            values[key] = default

    return values


def sync_ai_provider_env(
    *,
    include_recommended_defaults: bool = False,
    dry_run: bool = False,
    trigger_deploy: bool = False,
) -> dict[str, Any]:
    values = collect_ai_env_values(include_recommended_defaults=include_recommended_defaults)
    missing_secret_keys = [key for key in SECRET_KEYS if not _clean(os.getenv(key))]
    secrets = _secret_values(values)

    summary: dict[str, Any] = {
        "ok": True,
        "dry_run": dry_run,
        "secret_values_printed": False,
        "keys": sorted(values.keys()),
        "missing_secret_keys": missing_secret_keys,
        "results": [],
    }

    if not values:
        summary.update(
            {
                "ok": False,
                "reason_code": "no_ai_env_values",
                "next_action": "set HUGGINGFACE_API_TOKEN and/or GEMINI_API_KEY in the process environment",
            }
        )
        return summary

    if dry_run:
        return summary

    config = dict(os.environ)
    if trigger_deploy:
        config["RENDER_ENV_SYNC_TRIGGER_DEPLOY_ENABLED"] = "true"

    for key, value in values.items():
        result = render_env_sync.sync_render_env_var(key, value, config)
        safe_result = _redact(result, secrets)
        summary["results"].append(safe_result)
        if not safe_result.get("ok", False):
            summary["ok"] = False

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Sync Chatboc AI provider environment variables to Render. "
            "Secret values are read only from environment variables and are never printed."
        )
    )
    parser.add_argument(
        "--include-recommended-defaults",
        action="store_true",
        help="Also sync safe recommended non-secret defaults for Gemini, Hugging Face and Docling.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show key names that would be synced without calling Render.")
    parser.add_argument("--trigger-deploy", action="store_true", help="Ask Render to deploy after updating service env vars.")
    args = parser.parse_args()

    result = sync_ai_provider_env(
        include_recommended_defaults=args.include_recommended_defaults,
        dry_run=args.dry_run,
        trigger_deploy=args.trigger_deploy,
    )
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
