from typing import Iterable, Optional

from models import db, User, WhatsappNumero
from flask import current_app
from datetime import datetime

def update_user_profile(user: User, data: dict) -> bool:
    """
    Actualiza de forma segura el perfil de un usuario con los datos de un diccionario.
    Retorna True si fue exitoso, False en caso contrario.
    """
    if not data:
        current_app.logger.warning(f"No se proporcionaron datos para actualizar el perfil del usuario {user.id}")
        return False

    try:
        # Lista de campos permitidos para la actualización desde este servicio.
        # Excluimos explícitamente campos sensibles como rol, token, etc.
        allowed_fields = ['name', 'telefono', 'email', 'direccion', 'acepta_marketing']

        for key, value in data.items():
            if key in allowed_fields:
                # Lógica especial para 'acepta_marketing' para registrar la fecha
                if key == 'acepta_marketing':
                    new_value = bool(value)
                    if new_value and not user.acepta_marketing:
                        user.fecha_aceptacion_marketing = datetime.utcnow()
                    user.acepta_marketing = new_value
                else:
                    setattr(user, key, value)

        db.session.commit()
        current_app.logger.info(f"Perfil de usuario para {user.email} (ID: {user.id}) actualizado correctamente.")
        return True
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error al actualizar el perfil para el usuario {user.email} (ID: {user.id}): {e}", exc_info=True)
        return False


def assign_whatsapp_numbers(
    user: User,
    numbers: Optional[Iterable[str]],
    *,
    activate: bool = True,
    commit: bool = False,
):
    """Ensure ``numbers`` are linked to ``user`` via :class:`WhatsappNumero` entries.

    Args:
        user: Target :class:`User` owner.
        numbers: Iterable of raw phone numbers (e.g. ``+18564858589``).
        activate: When ``True`` reactivates inactive mappings as they are reassigned.
        commit: When ``True`` commits the current transaction, otherwise a session
            flush is issued so changes are visible to callers without persisting
            them immediately.

    Returns:
        List of dictionaries describing the operations performed for each number.
        Each dict includes the following keys: ``number``, ``mapping``, ``status``,
        ``previous_user_id``, ``previous_user_email`` and ``reactivated``.
    """

    if not user:
        raise ValueError("assign_whatsapp_numbers requiere un usuario válido")

    if not numbers:
        return []

    if user.id is None:
        db.session.flush()

    results: list[dict] = []

    for raw_number in numbers:
        number = (raw_number or "").strip()
        if not number:
            continue

        mapping = WhatsappNumero.query.filter_by(numero_whatsapp=number).first()
        previous_user_id = None
        previous_user_email = None
        status = "created"
        reactivated = False

        if mapping:
            previous_user_id = mapping.user_id
            previous_user_email = getattr(mapping.user, "email", None)
            was_active = bool(mapping.is_active)

            mapping.user_id = user.id
            if activate:
                mapping.is_active = True

            if previous_user_id == user.id:
                if not was_active and activate:
                    status = "reactivated"
                    reactivated = True
                else:
                    status = "updated"
            else:
                status = "reassigned"
                if not was_active and activate:
                    reactivated = True
        else:
            mapping = WhatsappNumero(
                numero_whatsapp=number,
                user_id=user.id,
                is_active=bool(activate),
            )
            db.session.add(mapping)

        results.append(
            {
                "number": number,
                "mapping": mapping,
                "status": status,
                "previous_user_id": previous_user_id,
                "previous_user_email": previous_user_email,
                "reactivated": reactivated,
            }
        )

    if commit:
        db.session.commit()
    else:
        db.session.flush()

    return results
