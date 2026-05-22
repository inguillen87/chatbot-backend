from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import quote

import os

import requests


CONTRACT_VERSION = "render.env_sync.v1"


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _bool_config(config: Mapping[str, Any], key: str, default: bool = False) -> bool:
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _read_config_or_env(config: Mapping[str, Any], key: str) -> str:
    return _clean(config.get(key)) or _clean(os.environ.get(key))


def _render_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _api_base(config: Mapping[str, Any]) -> str:
    return (_read_config_or_env(config, "RENDER_API_BASE_URL") or "https://api.render.com/v1").rstrip("/")


def _put_render_env_var(*, url: str, api_key: str, value: str, timeout: int) -> dict[str, Any]:
    response = requests.put(url, json={"value": value}, headers=_render_headers(api_key), timeout=timeout)
    try:
        body = response.json()
    except Exception:
        body = {"raw": response.text}
    if response.status_code >= 400:
        raise RuntimeError(f"render_api_error status={response.status_code} payload={body}")
    return body if isinstance(body, dict) else {"payload": body}


def _trigger_render_deploy(*, service_id: str, api_key: str, config: Mapping[str, Any], timeout: int) -> dict[str, Any]:
    url = f"{_api_base(config)}/services/{quote(service_id, safe='')}/deploys"
    response = requests.post(
        url,
        json={"clearCache": _read_config_or_env(config, "RENDER_ENV_SYNC_CLEAR_CACHE") or "do_not_clear"},
        headers=_render_headers(api_key),
        timeout=timeout,
    )
    try:
        body = response.json()
    except Exception:
        body = {"raw": response.text}
    if response.status_code >= 400:
        raise RuntimeError(f"render_api_error status={response.status_code} payload={body}")
    return body if isinstance(body, dict) else {"payload": body}


def sync_render_env_var(env_var_key: str, env_var_value: str, config: Mapping[str, Any]) -> dict[str, Any]:
    """Store a generated secret in Render without ever returning the secret value."""

    enabled = _bool_config(config, "RENDER_ENV_SYNC_ENABLED")
    service_id = _read_config_or_env(config, "RENDER_SERVICE_ID") or _read_config_or_env(config, "RENDER_BACKEND_SERVICE_ID")
    env_group_id = _read_config_or_env(config, "RENDER_ENV_GROUP_ID")
    api_key = _read_config_or_env(config, "RENDER_API_KEY")
    timeout_raw = _read_config_or_env(config, "RENDER_API_TIMEOUT_SECONDS")
    try:
        timeout = int(timeout_raw) if timeout_raw else 20
    except ValueError:
        timeout = 20

    result: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "enabled": enabled,
        "ok": True,
        "env_var_key": env_var_key,
        "secret_value_stored": False,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    if not enabled:
        result.update(
            {
                "mode": "disabled",
                "next_action": "set_render_env_sync_enabled",
                "required_env": ["RENDER_ENV_SYNC_ENABLED", "RENDER_API_KEY", "RENDER_SERVICE_ID or RENDER_ENV_GROUP_ID"],
            }
        )
        return result

    missing = []
    if not api_key:
        missing.append("RENDER_API_KEY")
    if not (service_id or env_group_id):
        missing.append("RENDER_SERVICE_ID or RENDER_ENV_GROUP_ID")
    if not env_var_key:
        missing.append("env_var_key")
    if not env_var_value:
        missing.append("env_var_value")
    if missing:
        result.update({"ok": False, "mode": "blocked", "reason_code": "render_env_sync_config_missing", "missing": missing})
        return result

    target = "env_group" if env_group_id else "service"
    target_id = env_group_id or service_id
    quoted_key = quote(env_var_key, safe="")
    if env_group_id:
        url = f"{_api_base(config)}/env-groups/{quote(env_group_id, safe='')}/env-vars/{quoted_key}"
    else:
        url = f"{_api_base(config)}/services/{quote(service_id, safe='')}/env-vars/{quoted_key}"

    try:
        payload = _put_render_env_var(url=url, api_key=api_key, value=env_var_value, timeout=timeout)
    except Exception as exc:
        result.update(
            {
                "ok": False,
                "mode": "failed",
                "target": target,
                "target_id": target_id,
                "reason_code": "render_env_var_upsert_failed",
                "error": str(exc),
            }
        )
        return result

    result.update(
        {
            "mode": "live",
            "target": target,
            "target_id": target_id,
            "secret_value_stored": True,
            "response_keys": sorted(payload.keys()),
        }
    )

    if service_id and _bool_config(config, "RENDER_ENV_SYNC_TRIGGER_DEPLOY_ENABLED"):
        try:
            deploy_payload = _trigger_render_deploy(
                service_id=service_id,
                api_key=api_key,
                config=config,
                timeout=timeout,
            )
            result["deploy"] = {
                "triggered": True,
                "response_keys": sorted(deploy_payload.keys()),
                "id": deploy_payload.get("id") or deploy_payload.get("deploy", {}).get("id"),
            }
        except Exception as exc:
            result["deploy"] = {
                "triggered": False,
                "reason_code": "render_deploy_trigger_failed",
                "error": str(exc),
            }
    else:
        result["deploy"] = {
            "triggered": False,
            "reason_code": "disabled_or_missing_service_id",
        }

    return result
