"""Canonical, fail-closed ticket workflow helpers.

The CRM historically accepted a mix of Spanish and English status aliases.
This module keeps those aliases readable at the API boundary while enforcing a
single transition graph for every write path.
"""

from __future__ import annotations

from typing import Any, Callable


TICKET_ALLOWED_STATES = [
    "nuevo",
    "en_proceso",
    "en_vivo",
    "esperando_agente_en_vivo",
    "cerrado",
]

TICKET_ALLOWED_TRANSITIONS = {
    "nuevo": ["en_proceso", "cerrado"],
    "en_proceso": ["en_vivo", "esperando_agente_en_vivo", "cerrado"],
    "en_vivo": ["en_proceso", "cerrado"],
    "esperando_agente_en_vivo": ["en_vivo", "en_proceso", "cerrado"],
    "cerrado": [],
}

TICKET_FINAL_STATES = {"cerrado"}

_TICKET_STATE_ALIASES = {
    "nuevo": "nuevo",
    "open": "nuevo",
    "abierto": "en_proceso",
    "en_espera": "en_proceso",
    "en_proceso": "en_proceso",
    "in_progress": "en_proceso",
    "waiting_customer": "en_proceso",
    "en_vivo": "en_vivo",
    "esperando_agente": "esperando_agente_en_vivo",
    "esperando_agente_en_vivo": "esperando_agente_en_vivo",
    "resuelto": "cerrado",
    "resolved": "cerrado",
    "cerrado": "cerrado",
    "closed": "cerrado",
}


def _normalize_token(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", " ").replace(" ", "_")


def normalize_ticket_state(value: Any) -> str | None:
    """Return a canonical state or ``None`` for unknown input."""

    token = _normalize_token(value)
    return _TICKET_STATE_ALIASES.get(token)


def is_ticket_transition_allowed(current_state: Any, requested_state: Any) -> bool:
    current = normalize_ticket_state(current_state)
    requested = normalize_ticket_state(requested_state)
    if not current or not requested:
        return False
    if current == requested:
        return True
    return requested in TICKET_ALLOWED_TRANSITIONS.get(current, [])


def build_ticket_workflow_instance(
    current_state: Any,
    *,
    can_operate: bool,
    blocked_reason: str | None = None,
    externalize: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Build the per-ticket transition contract published to an operator.

    Unknown states and unauthorized actors receive no transitions. This makes
    old/corrupt data fail closed instead of giving the UI a guessed action.
    """

    canonical_state = normalize_ticket_state(current_state)
    transform = externalize or (lambda value: value)
    if canonical_state is None:
        return {
            "contract_version": "ticket.workflow.instance.v2",
            "current_state": str(current_state or ""),
            "canonical_state": None,
            "next_states": [],
            "can_transition": False,
            "final_state": False,
            "blocked_reason": blocked_reason or "ticket_state_unknown",
        }

    final_state = canonical_state in TICKET_FINAL_STATES
    if not can_operate:
        next_states: list[str] = []
        reason = blocked_reason or "ticket_transition_forbidden"
    elif final_state:
        next_states = []
        reason = "ticket_final_state"
    else:
        next_states = [
            transform(state)
            for state in TICKET_ALLOWED_TRANSITIONS.get(canonical_state, [])
        ]
        reason = None

    return {
        "contract_version": "ticket.workflow.instance.v2",
        "current_state": transform(canonical_state),
        "canonical_state": canonical_state,
        "next_states": next_states,
        "can_transition": bool(next_states),
        "final_state": final_state,
        "blocked_reason": reason,
    }
