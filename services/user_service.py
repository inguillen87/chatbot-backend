from typing import Iterable, Optional, Tuple

from models import db, User, WhatsappNumero
from flask import current_app, g
from datetime import datetime
from sqlalchemy import func

PASSWORD_MIN_LENGTH = 8
PASSWORD_RESET_TOKEN_TTL_SECONDS = 3600


def _validate_password_strength(password: str) -> Tuple[bool, str]:
    if not password or len(password) < PASSWORD_MIN_LENGTH:
        return False, f"La contraseña debe tener al menos {PASSWORD_MIN_LENGTH} caracteres."

    if password.isdigit() or password.isalpha():
        return False, "La contraseña debe incluir letras y números."

    return True, ""


def split_password_reset_token(raw_token: Optional[str]) -> Optional[Tuple[str, str]]:
    if not raw_token or "." not in raw_token:
        return None

    selector, verifier = raw_token.split(".", 1)
    selector = selector.strip()
    verifier = verifier.strip()

    if not selector or not verifier:
        return None

    return selector, verifier


def create_password_reset_request(user: User, *, commit: bool = True) -> str:
    if not user:
        raise ValueError("Se requiere un usuario válido para generar un reseteo de contraseña")

    token = user.generate_password_reset_token()

    if commit:
        db.session.commit()
    else:
        db.session.flush()

    current_app.logger.info(
        "[user_service] Generado token de reseteo para el usuario %s", getattr(user, "id", None)
    )

    return token


def reset_password_with_token(
    selector: str,
    verifier: str,
    new_password: str,
    *,
    commit: bool = True,
    max_age_seconds: int = PASSWORD_RESET_TOKEN_TTL_SECONDS,
) -> Tuple[bool, str, int]:
    if not selector or not verifier:
        return False, "Token inválido.", 400

    user = User.query.filter_by(password_reset_selector=selector).first()
    if not user:
        return False, "Token inválido o expirado.", 400

    if not user.verify_password_reset_token(verifier, max_age_seconds):
        return False, "Token inválido o expirado.", 400

    is_valid, message = _validate_password_strength(new_password)
    if not is_valid:
        return False, message, 400

    try:
        user.set_password(new_password)
        user.clear_password_reset_token()
        if commit:
            db.session.commit()
    except Exception as exc:
        current_app.logger.exception(
            "[user_service] Error al aplicar reseteo de contraseña para el usuario %s", user.id
        )
        if commit:
            db.session.rollback()
        return False, "No se pudo actualizar la contraseña.", 500

    return True, "Contraseña actualizada correctamente.", 200


def change_user_email(
    user: User,
    new_email: Optional[str],
    current_password: Optional[str],
    *,
    commit: bool = True,
) -> Tuple[bool, str, int]:
    normalized_email = (new_email or "").strip().lower()
    if not normalized_email:
        return False, "El nuevo email es requerido.", 400

    if not current_password:
        return False, "La contraseña actual es requerida para cambiar el email.", 400

    if not user.check_password(current_password):
        return False, "La contraseña actual es incorrecta.", 400

    if normalized_email == user.email:
        return False, "El nuevo email debe ser diferente al actual.", 400

    existing = (
        User.query.filter(func.lower(User.email) == normalized_email)
        .filter(User.id != user.id)
        .first()
    )
    if existing:
        return False, "El email ingresado ya está en uso.", 400

    original_email = user.email
    user.email = normalized_email

    if not commit:
        return True, "Email actualizado correctamente.", 200

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        user.email = original_email
        current_app.logger.exception(
            "[user_service] Error al cambiar email del usuario %s", getattr(user, "id", None)
        )
        return False, "No se pudo actualizar el email en este momento.", 500

    return True, "Email actualizado correctamente.", 200


def change_user_password(
    user: User,
    current_password: Optional[str],
    new_password: Optional[str],
    *,
    commit: bool = True,
) -> Tuple[bool, str, int]:
    if not current_password or not new_password:
        return False, "La contraseña actual y la nueva contraseña son requeridas.", 400

    if not user.check_password(current_password):
        return False, "La contraseña actual es incorrecta.", 400

    if current_password == new_password:
        return False, "La nueva contraseña debe ser distinta a la actual.", 400

    is_valid, message = _validate_password_strength(new_password)
    if not is_valid:
        return False, message, 400

    try:
        user.set_password(new_password)
        if commit:
            db.session.commit()
    except Exception:
        if commit:
            db.session.rollback()
        current_app.logger.exception(
            "[user_service] Error al cambiar la contraseña del usuario %s", getattr(user, "id", None)
        )
        return False, "No se pudo actualizar la contraseña.", 500

    return True, "Contraseña actualizada correctamente.", 200


def update_user_profile(user: User, data: dict) -> bool:
    """
    Actualiza de forma segura el perfil de un usuario con los datos de un diccionario.
    Retorna True si fue exitoso, False en caso contrario.
    """
    if not data:
        current_app.logger.warning(
            f"No se proporcionaron datos para actualizar el perfil del usuario {user.id}"
        )
        g.profile_update_error = ("No se recibieron datos para actualizar el perfil.", 400)
        return False

    email_value = data.get("email")
    if email_value is not None and email_value != user.email:
        success, message, status = change_user_email(
            user,
            email_value,
            data.get("current_password"),
            commit=False,
        )
        if not success:
            g.profile_update_error = (message, status)
            return False

    try:
        # Lista de campos permitidos para la actualización desde este servicio.
        # Excluimos explícitamente campos sensibles como rol, token, etc.
        allowed_fields = ['name', 'telefono', 'direccion', 'acepta_marketing']

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
        current_app.logger.info(
            f"Perfil de usuario para {user.email} (ID: {user.id}) actualizado correctamente."
        )
        return True
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(
            f"Error al actualizar el perfil para el usuario {user.email} (ID: {user.id}): {e}",
            exc_info=True,
        )
        g.profile_update_error = ("Error interno al guardar el perfil.", 500)
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
