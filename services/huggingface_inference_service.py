import logging
import os
from datetime import datetime, timezone
import time
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

_LAST_FAILURE: dict[str, Any] = {}
_LAST_WARNING_LOGGED_AT: dict[str, float] = {}


def _env_first(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return default


def _truthy_env(*names: str) -> bool:
    return any(str(os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"} for name in names)


def _api_token() -> str | None:
    return _env_first("HUGGINGFACE_API_TOKEN", "HF_TOKEN")


def _redact_sensitive(value: str) -> str:
    redacted = value
    for secret in (_api_token(), os.getenv("HUGGINGFACE_API_TOKEN"), os.getenv("HF_TOKEN")):
        if secret and secret in redacted:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted


def _classify_provider_error(exc: Exception) -> dict[str, str]:
    message = _redact_sensitive(str(exc))
    lower_message = message.lower()
    error_type = type(exc).__name__

    if "402" in message or "payment required" in lower_message or "credits" in lower_message:
        return {
            "reason_code": "huggingface_quota_or_payment_required",
            "error_type": error_type,
            "severity": "warning",
        }
    if "429" in message or "rate limit" in lower_message or "too many requests" in lower_message:
        return {
            "reason_code": "huggingface_rate_limited",
            "error_type": error_type,
            "severity": "warning",
        }
    if "401" in message or "403" in message or "unauthorized" in lower_message or "forbidden" in lower_message:
        return {
            "reason_code": "huggingface_auth_failed",
            "error_type": error_type,
            "severity": "warning",
        }
    if "timeout" in lower_message or "timed out" in lower_message:
        return {
            "reason_code": "huggingface_timeout",
            "error_type": error_type,
            "severity": "warning",
        }
    if "500" in message or "502" in message or "503" in message or "504" in message:
        return {
            "reason_code": "huggingface_provider_unavailable",
            "error_type": error_type,
            "severity": "warning",
        }
    return {
        "reason_code": "huggingface_provider_call_failed",
        "error_type": error_type,
        "severity": "error",
    }


def _record_failure(task: str, exc: Exception) -> dict[str, Any]:
    diagnostic = _classify_provider_error(exc)
    message = _redact_sensitive(str(exc))
    _LAST_FAILURE.clear()
    _LAST_FAILURE.update(
        {
            "task": task,
            "reason_code": diagnostic["reason_code"],
            "error_type": diagnostic["error_type"],
            "severity": diagnostic["severity"],
            "message": message[:240],
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    return dict(_LAST_FAILURE)


def clear_last_huggingface_failure() -> None:
    _LAST_FAILURE.clear()
    _LAST_WARNING_LOGGED_AT.clear()


def get_last_huggingface_failure() -> dict[str, Any] | None:
    return dict(_LAST_FAILURE) if _LAST_FAILURE else None


def _log_provider_failure(operation: str, exc: Exception) -> None:
    failure = _record_failure(operation, exc)
    if failure.get("severity") == "warning":
        signature = f"{operation}:{failure.get('reason_code')}:{failure.get('error_type')}"
        now = time.monotonic()
        cooldown_seconds = 300.0
        last_logged_at = _LAST_WARNING_LOGGED_AT.get(signature)
        if last_logged_at is not None and now - last_logged_at < cooldown_seconds:
            return
        _LAST_WARNING_LOGGED_AT[signature] = now
        logger.warning(
            "Hugging Face %s degraded: %s (%s)",
            operation,
            failure.get("reason_code"),
            failure.get("error_type"),
        )
        return
    logger.error("Hugging Face %s failed: %s", operation, exc, exc_info=True)


def huggingface_configured() -> bool:
    return bool(_api_token())


def _timeout() -> float:
    try:
        return float(os.getenv("HUGGINGFACE_TIMEOUT_SECONDS", "5"))
    except ValueError:
        return 5.0


def _provider() -> str:
    return os.getenv("HUGGINGFACE_PROVIDER", "auto").strip() or "auto"


def _network_calls_allowed() -> bool:
    """Keep unit and request tests deterministic unless they explicitly opt in."""

    try:
        from flask import current_app, has_app_context
    except Exception:  # pragma: no cover - Flask is always available in the API
        return True

    if not has_app_context() or not current_app.config.get("TESTING"):
        return True

    return bool(current_app.config.get("HUGGINGFACE_ALLOW_NETWORK_IN_TESTS")) or _truthy_env(
        "HUGGINGFACE_ALLOW_NETWORK_IN_TESTS",
        "HF_ALLOW_NETWORK_IN_TESTS",
    )


def _get_client(model: str | None = None):
    token = _api_token()
    if not token:
        raise ConnectionError("HUGGINGFACE_API_TOKEN is not configured.")
    try:
        from huggingface_hub import InferenceClient
    except Exception as exc:  # pragma: no cover - depends on deploy env
        raise ConnectionError("huggingface_hub is not installed.") from exc
    return InferenceClient(model=model, provider=_provider(), token=token, timeout=_timeout())


def _to_plain_list(value: Any) -> list:
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, tuple):
        value = list(value)
    if isinstance(value, list):
        return value
    return [value]


def _flatten_embedding(raw_embedding: Any) -> list[float]:
    value = _to_plain_list(raw_embedding)
    while value and isinstance(value[0], list):
        value = value[0]
    return [float(item) for item in value]


def _as_dict(item: Any) -> dict:
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        return item.model_dump()
    if hasattr(item, "_asdict"):
        return item._asdict()
    return {
        key: getattr(item, key)
        for key in ("label", "score", "box", "summary_text", "generated_text")
        if hasattr(item, key)
    }


def embeddings_enabled() -> bool:
    return _truthy_env("HUGGINGFACE_EMBEDDINGS_ENABLED", "HF_EMBEDDINGS_ENABLED")


def vision_enabled() -> bool:
    return _truthy_env("VISION_HUGGINGFACE_ENABLED", "HUGGINGFACE_VISION_ENABLED")


def zero_shot_enabled() -> bool:
    return _truthy_env("HUGGINGFACE_ZERO_SHOT_ENABLED", "HF_ZERO_SHOT_ENABLED")


def embed_texts(
    textos: Iterable[str],
    *,
    model: str | None = None,
    normalize: bool = True,
    expected_dimension: int | None = None,
) -> Optional[list[list[float]]]:
    if not embeddings_enabled() or not _network_calls_allowed():
        return None

    cleaned = [str(text).replace("\n", " ").strip() for text in textos if isinstance(text, str) and text.strip()]
    if not cleaned:
        return None

    resolved_model = model or os.getenv("HUGGINGFACE_EMBEDDING_MODEL", "intfloat/multilingual-e5-large")

    try:
        client = _get_client(model=resolved_model)
        vectors: list[list[float]] = []
        for text in cleaned:
            vector = _flatten_embedding(
                client.feature_extraction(text, model=resolved_model, normalize=normalize)
            )
            if expected_dimension and len(vector) != expected_dimension:
                logger.warning(
                    "Hugging Face embedding dimension mismatch for %s: got %s, expected %s",
                    resolved_model,
                    len(vector),
                    expected_dimension,
                )
                return None
            vectors.append(vector)
        return vectors
    except Exception as exc:
        _log_provider_failure("embeddings", exc)
        return None


def classify_zero_shot(
    text: str,
    labels: list[str],
    *,
    model: str | None = None,
    multi_label: bool = False,
) -> Optional[list[dict]]:
    if not zero_shot_enabled() or not _network_calls_allowed() or not text or not labels:
        return None

    resolved_model = model or os.getenv("HUGGINGFACE_ZERO_SHOT_MODEL", "joeddav/xlm-roberta-large-xnli")

    try:
        client = _get_client(model=resolved_model)
        result = client.zero_shot_classification(
            text,
            candidate_labels=labels,
            multi_label=multi_label,
            model=resolved_model,
        )
        return sorted(
            [_as_dict(item) for item in result],
            key=lambda item: float(item.get("score") or 0),
            reverse=True,
        )
    except Exception as exc:
        _log_provider_failure("zero_shot", exc)
        return None


def classify_image(image_bytes: bytes, *, model: str | None = None, top_k: int = 5) -> Optional[list[dict]]:
    if not vision_enabled() or not _network_calls_allowed() or not image_bytes:
        return None

    resolved_model = model or os.getenv("HUGGINGFACE_IMAGE_CLASSIFICATION_MODEL", "google/vit-base-patch16-224")

    try:
        client = _get_client(model=resolved_model)
        result = client.image_classification(image_bytes, model=resolved_model, top_k=top_k)
        return [_as_dict(item) for item in result]
    except Exception as exc:
        _log_provider_failure("image_classification", exc)
        return None


def detect_objects(image_bytes: bytes, *, model: str | None = None, threshold: float | None = None) -> Optional[list[dict]]:
    if not vision_enabled() or not _network_calls_allowed() or not image_bytes:
        return None

    resolved_model = model or os.getenv("HUGGINGFACE_OBJECT_DETECTION_MODEL", "facebook/detr-resnet-50")
    resolved_threshold = threshold
    if resolved_threshold is None:
        try:
            resolved_threshold = float(os.getenv("HUGGINGFACE_OBJECT_DETECTION_THRESHOLD", "0.60"))
        except ValueError:
            resolved_threshold = 0.60

    try:
        client = _get_client(model=resolved_model)
        result = client.object_detection(image_bytes, model=resolved_model, threshold=resolved_threshold)
        return [_as_dict(item) for item in result]
    except Exception as exc:
        _log_provider_failure("object_detection", exc)
        return None


def image_to_text(image_bytes: bytes, *, model: str | None = None) -> Optional[str]:
    if not vision_enabled() or not _network_calls_allowed() or not image_bytes:
        return None

    resolved_model = model or os.getenv("HUGGINGFACE_IMAGE_TO_TEXT_MODEL", "Salesforce/blip-image-captioning-base")

    try:
        client = _get_client(model=resolved_model)
        result = client.image_to_text(image_bytes, model=resolved_model)
        payload = _as_dict(result)
        text = (
            payload.get("generated_text")
            or payload.get("text")
            or payload.get("summary_text")
            or payload.get("label")
            or ""
        )
        return str(text).strip() or None
    except Exception as exc:
        _log_provider_failure("image_to_text", exc)
        return None


def analyze_image_for_chatboc(image_bytes: bytes) -> Optional[dict]:
    labels = classify_image(image_bytes) or []
    objects = detect_objects(image_bytes) or []
    text = image_to_text(image_bytes) or ""

    if not labels and not objects and not text:
        return None

    return {
        "labels": [str(item.get("label") or "") for item in labels if item.get("label")],
        "objects": [str(item.get("label") or "") for item in objects if item.get("label")],
        "text": text,
    }
