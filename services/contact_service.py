import logging
import re
from typing import Any, Dict, Optional

from services.common_utils import formatear_telefono_e164
from models import User

logger = logging.getLogger(__name__)


def _sanitize_profile_name(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    cleaned = str(value).strip()
    if len(cleaned) < 2:
        return None
    lowered = cleaned.lower()
    banned = {"hola", "buenas", "eh", "mmm", "vecino", "vecino/a", "usuario", "anonimo"}
    if lowered in banned:
        return None
    words = re.findall(r"[a-záéíóúñ]+", lowered)
    greetings = {"hola", "buenas", "buenos", "buen"}
    if words and all(word in greetings for word in words):
        return None
    return cleaned


def sanitize_profile_name(value: Optional[str]) -> Optional[str]:
    return _sanitize_profile_name(value)


def resolve_contact(phone: Optional[str], profile_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Resolve a contact from DB and/or profile name, returning normalized info.
    """
    resolved = {
        "nombre": None,
        "email": None,
        "dni": None,
        "telefono": None,
        "user_id": None,
        "is_known_contact": False,
    }

    telefono_norm = None
    if phone:
        try:
            telefono_norm = formatear_telefono_e164(phone)
        except Exception:
            telefono_norm = str(phone).strip()

    if telefono_norm:
        resolved["telefono"] = telefono_norm
        user = (
            User.query.filter(User.telefono == telefono_norm).first()
            if telefono_norm
            else None
        )
        if user:
            raw_name = getattr(user, "name", None) or getattr(user, "nombre", None)
            resolved.update(
                {
                    "nombre": sanitize_profile_name(raw_name),
                    "email": getattr(user, "email", None),
                    "dni": getattr(user, "dni_vecino", None) or getattr(user, "dni", None),
                    "user_id": getattr(user, "id", None),
                    "is_known_contact": True,
                }
            )

    if not resolved.get("nombre"):
        resolved["nombre"] = sanitize_profile_name(profile_name)

    return resolved
