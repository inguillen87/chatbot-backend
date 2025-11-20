from __future__ import annotations

from typing import Any, Dict

from flask import session


def _storage() -> Dict[str, Dict[str, Any]]:
    data = session.get("demo_reward_profiles")
    if not isinstance(data, dict):
        data = {}
        session["demo_reward_profiles"] = data
    return data


def _bootstrap_profile() -> Dict[str, Any]:
    return {
        "saldo_demo_puntos": 12500,
        "bonos_bienvenida": 500,
        "encuestas_completadas": 7,
        "sondeos_rapidos": 3,
        "reclamos_resueltos": 1,
        "pendiente_canje": 0.0,
    }


def _ensure_profile(tenant_id: int) -> Dict[str, Any]:
    store = _storage()
    key = str(tenant_id)
    profile = store.get(key)
    if not isinstance(profile, dict):
        profile = _bootstrap_profile()
        store[key] = profile
        session.modified = True
    return profile


def reward_profile_for_tenant(tenant_id: int, puntos_en_carrito: float = 0.0) -> Dict[str, Any]:
    profile = dict(_ensure_profile(tenant_id))
    profile["pendiente_canje"] = max(puntos_en_carrito, 0.0)

    saldo_total = float(profile.get("saldo_demo_puntos", 0)) + float(profile.get("bonos_bienvenida", 0))
    saldo_restante = max(saldo_total - profile["pendiente_canje"], 0.0)

    profile["balance_resumen"] = {
        "saldo_disponible": round(saldo_total, 2),
        "puntos_en_carrito": round(profile["pendiente_canje"], 2),
        "saldo_estimado_post_compra": round(saldo_restante, 2),
    }
    profile["saldo_despues_de_carrito"] = profile["balance_resumen"]["saldo_estimado_post_compra"]
    return profile

