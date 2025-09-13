from typing import Any, Dict, List

REQUIRED_FIELDS = ["categoria", "descripcion", "lat", "lng", "direccion", "nombre", "telefono", "email"]


def get_ticket_draft(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure ticket_draft exists in context and return it."""
    return ctx.setdefault(
        "ticket_draft",
        {
            "categoria": None,
            "descripcion": None,
            "lat": None,
            "lng": None,
            "direccion": None,
            "nombre": None,
            "telefono": None,
            "email": None,
            "adjuntos": [],
        },
    )


def merge_ticket_fields(ctx: Dict[str, Any], new_fields: Dict[str, Any]) -> bool:
    """Merge extracted fields into ticket_draft without overwriting existing values.

    Returns True if the draft was modified.
    """
    draft = get_ticket_draft(ctx)
    pending = ctx.setdefault("ticket_draft_pending", {})
    changed = False

    if not isinstance(new_fields, dict):
        return False

    for key, value in new_fields.items():
        if key not in draft or value in (None, ""):
            continue
        if key == "adjuntos":
            items: List[Any] = value if isinstance(value, list) else [value]
            for item in items:
                if item not in draft["adjuntos"]:
                    draft["adjuntos"].append(item)
                    changed = True
        else:
            if draft.get(key) is None:
                draft[key] = value
                changed = True
            elif draft.get(key) != value:
                pending[key] = value

    if changed:
        update_confirmation_state(ctx)
    return changed


def is_ticket_draft_complete(draft: Dict[str, Any]) -> bool:
    """Return True if all required fields are present in draft."""
    return all(draft.get(field) not in (None, "") for field in REQUIRED_FIELDS)


def update_confirmation_state(ctx: Dict[str, Any]) -> None:
    """Set a flag when the draft is complete and ready for confirmation."""
    draft = get_ticket_draft(ctx)
    if is_ticket_draft_complete(draft):
        ctx["ticket_draft_ready"] = True
