"""Configuration compatibility for notification routes; does not dispatch messages."""
from copy import deepcopy
import json
import math
from typing import Any


class NotificationSettingsError(ValueError):
    pass


class NotificationSettingsRefreshRequired(NotificationSettingsError):
    pass


def _json_value(value: Any) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _json_value(item) for key, item in value.items())
    return False


def _preferences(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not _json_value(value):
        raise NotificationSettingsError("preferences debe ser un objeto JSON valido.")
    for channel in ("email", "telegram", "whatsapp"):
        if channel in value and not isinstance(value[channel], bool):
            raise NotificationSettingsError(f"preferences.{channel} debe ser booleano.")
    return deepcopy(value)


def _delivery(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not _json_value(value):
        raise NotificationSettingsError("delivery debe ser un objeto JSON valido.")
    for field in ("owner_phone", "telegram_chat_id"):
        if field in value and value[field] is not None and not isinstance(value[field], str):
            raise NotificationSettingsError(f"delivery.{field} debe ser texto o null.")
    return deepcopy(value)


def _triggers(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value) or not _json_value(value):
        raise NotificationSettingsError("triggers debe ser una lista de objetos JSON.")
    for item in value:
        for field in ("event", "channel"):
            if field in item and not isinstance(item[field], str):
                raise NotificationSettingsError(f"triggers.{field} debe ser texto.")
    return deepcopy(value)


def parse_notification_update(payload: Any, *, legacy: bool = False) -> dict[str, Any]:
    """Validate the full body before any configuration mutation or query for ACK data."""
    if not isinstance(payload, dict):
        raise NotificationSettingsError("El body debe ser un objeto JSON.")
    allowed = (
        {"notification_settings", "owner_phone", "telegram_chat_id"}
        if legacy else {"preferences", "triggers", "delivery", "delivery_config", "legacy_payload"}
    )
    if set(payload) - allowed:
        raise NotificationSettingsError("El body contiene campos no admitidos.")

    update = {}
    if legacy:
        if "notification_settings" in payload:
            update["preferences"] = _preferences(payload["notification_settings"])
        delivery = {key: payload[key] for key in ("owner_phone", "telegram_chat_id") if key in payload}
        if delivery:
            update["delivery"] = _delivery(delivery)
        return update

    if "preferences" in payload:
        update["preferences"] = _preferences(payload["preferences"])
    if "triggers" in payload:
        update["triggers"] = _triggers(payload["triggers"])
    canonical = _delivery(payload["delivery"]) if "delivery" in payload else {}
    alias = _delivery(payload["delivery_config"]) if "delivery_config" in payload else {}
    if any(
        key in canonical
        and json.dumps(canonical[key], sort_keys=True) != json.dumps(value, sort_keys=True)
        for key, value in alias.items()
    ):
        raise NotificationSettingsError("delivery y delivery_config contienen valores contradictorios.")
    if "delivery" in payload or "delivery_config" in payload:
        update["delivery"] = {**alias, **canonical}
    # Older frontend versions include a copy of their draft here. It is inert:
    # the validated preferences/delivery fields alone are the update authority.
    if "legacy_payload" in payload and not isinstance(payload["legacy_payload"], dict):
        raise NotificationSettingsError("legacy_payload debe ser un objeto.")
    return update


def notification_hooks_snapshot(config: Any) -> dict[str, Any]:
    """Canonical fields win; legacy values bridge only absent canonical fields."""
    if config is not None and not isinstance(config, dict):
        raise NotificationSettingsError("configuracion almacenada no es un objeto.")
    config = config if config is not None else {}
    raw_hooks = config.get("notification_hooks", {})
    if not isinstance(raw_hooks, dict):
        raise NotificationSettingsError("notification_hooks almacenado no es un objeto.")
    hooks = deepcopy(raw_hooks)
    hooks["preferences"] = _preferences(
        raw_hooks["preferences"] if "preferences" in raw_hooks else config.get("notification_settings", {})
    )
    delivery = _delivery(raw_hooks.get("delivery", {}))
    for field, legacy_key in (
        ("owner_phone", "owner_notification_phone"),
        ("telegram_chat_id", "owner_telegram_chat_id"),
    ):
        if field not in delivery:
            delivery[field] = config.get(legacy_key)
    hooks["delivery"] = _delivery(delivery)
    hooks["triggers"] = _triggers(raw_hooks.get("triggers", []))
    return hooks


def guard_notification_alias_clears(config: Any, payload: dict[str, Any]) -> None:
    """An old frontend may have a blank draft from before it could read delivery.

    Only canonical delivery or the legacy PUT can intentionally clear a saved
    destination. Alias-only blanks require a refresh to avoid overwriting a
    nonempty target from a stale draft during the frontend/backend upgrade.
    """
    alias = payload.get("delivery_config", {})
    canonical = payload.get("delivery", {})
    clearing = [
        field for field in ("owner_phone", "telegram_chat_id")
        if field in alias and alias[field] in (None, "") and field not in canonical
    ]
    if clearing:
        delivery = notification_hooks_snapshot(config)["delivery"]
        if any(delivery[field] not in (None, "") for field in clearing):
            raise NotificationSettingsRefreshRequired("Actualiza la configuracion antes de guardar los destinos.")


def merge_notification_update(config: Any, update: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Sparse preferences/delivery merge, explicit triggers replacement, independent JSON copies."""
    hooks = notification_hooks_snapshot(config)
    config = deepcopy(config) if config is not None else {}
    for field in ("preferences", "delivery"):
        if field in update:
            hooks[field] = {**hooks[field], **deepcopy(update[field])}
    if "triggers" in update:
        hooks["triggers"] = deepcopy(update["triggers"])
    config["notification_hooks"] = hooks
    # Keep legacy route reads/writes coherent during client upgrades. These JSON
    # keys are route configuration, not the dispatcher's TenantProfile columns.
    legacy_preferences = _preferences(config.get("notification_settings", {}))
    # Preserve legacy-only extras as stored data. They do not become read
    # authority while an explicit canonical preferences object exists.
    config["notification_settings"] = {**legacy_preferences, **deepcopy(hooks["preferences"])}
    config["owner_notification_phone"] = hooks["delivery"]["owner_phone"]
    config["owner_telegram_chat_id"] = hooks["delivery"]["telegram_chat_id"]
    return config, hooks
