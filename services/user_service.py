from typing import Iterable, Optional, Tuple
from urllib.parse import urlparse

from models import db, User, WhatsappNumero
from flask import current_app, g
from datetime import datetime
from sqlalchemy import func
from sqlalchemy.orm.attributes import flag_modified

PASSWORD_MIN_LENGTH = 8
PASSWORD_RESET_TOKEN_TTL_SECONDS = 3600
PROFILE_AVATAR_MAX_LENGTH = 512
PROFILE_AVATAR_SOURCES = {
    "profile_url",
    "profile_upload",
    "profile_picture",
    "user_upload",
    "google",
    "clerk",
    "facebook",
    "linkedin",
    "oauth",
    "social_login",
}
BLOCKED_AVATAR_SOURCE_KEYWORDS = {
    "no_consent",
    "without_consent",
    "not_consented",
    "unconsented",
    "consent_denied",
    "consent_rejected",
    "whatsapp_profile",
    "whatsapp_avatar",
    "whatsapp_photo",
    "wa_profile",
    "wa_avatar",
    "scrape",
    "scraping",
    "scraped",
    "profile_scrape",
    "mock",
    "fake",
    "synthetic",
    "realistic_generated",
}


def _profile_metadata(user: User) -> dict:
    return user.accesibilidad if isinstance(getattr(user, "accesibilidad", None), dict) else {}


def _normalize_avatar_source(value: Optional[str], fallback: str = "profile_url") -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return fallback
    return normalized if normalized in PROFILE_AVATAR_SOURCES else ""


def _has_blocked_avatar_source(value: Optional[str]) -> bool:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return False
    return any(keyword in normalized for keyword in BLOCKED_AVATAR_SOURCE_KEYWORDS)


def _avatar_consent_denied(value: object) -> bool:
    if value is False:
        return True
    if isinstance(value, (int, float)):
        return value == 0
    if isinstance(value, str):
        return value.strip().lower() in {
            "0",
            "false",
            "no",
            "n",
            "denied",
            "rejected",
            "unconsented",
            "sin_consentimiento",
        }
    return False


def normalize_profile_avatar_url(value: object) -> Tuple[bool, Optional[str], str]:
    """Return a safe profile avatar URL or a validation error.

    We only persist consented/profile URLs. WhatsApp scraping, data URLs and
    executable schemes are deliberately rejected.
    """

    if value is None:
        return True, None, ""
    if not isinstance(value, str):
        return False, None, "La imagen de perfil debe ser una URL."

    avatar_url = value.strip()
    if not avatar_url:
        return True, None, ""
    if len(avatar_url) > PROFILE_AVATAR_MAX_LENGTH:
        return False, None, "La URL de imagen de perfil es demasiado larga."

    if avatar_url.startswith("/") and not avatar_url.startswith("//") and "\\" not in avatar_url:
        allowed_prefixes = ("/uploads/", "/media/", "/static/", "/avatars/", "/profile/")
        if avatar_url.startswith(allowed_prefixes):
            return True, avatar_url, ""
        return False, None, "Usa una URL HTTPS o una ruta interna de imagen permitida."

    parsed = urlparse(avatar_url)
    scheme = (parsed.scheme or "").lower()
    hostname = (parsed.hostname or "").lower()
    if scheme == "https" and hostname:
        return True, avatar_url, ""
    if scheme == "http" and hostname in {"localhost", "127.0.0.1", "::1"}:
        return True, avatar_url, ""

    return False, None, "La imagen de perfil debe usar HTTPS o una ruta interna segura."


def get_user_profile_identity(user: User) -> dict:
    metadata = _profile_metadata(user)
    identity = metadata.get("identity") if isinstance(metadata.get("identity"), dict) else {}
    avatar_url = identity.get("avatar_url") or metadata.get("profile_avatar_url")
    raw_avatar_source = identity.get("avatar_source") or metadata.get("profile_avatar_source")
    avatar_source = str(raw_avatar_source).strip() if raw_avatar_source else ("profile_url" if avatar_url else None)
    normalized_source = str(avatar_source or "").strip().lower()
    explicit_consent = identity.get("avatar_consent")
    avatar_consent = bool(
        avatar_url
        and normalized_source in PROFILE_AVATAR_SOURCES
        and explicit_consent is not False
    )
    return {
        "avatar_url": str(avatar_url).strip() if avatar_url else None,
        "avatar_source": avatar_source,
        "avatar_consent": avatar_consent,
    }


def set_user_profile_avatar(
    user: User,
    raw_avatar_url: object,
    *,
    source: str = "profile_url",
    overwrite: bool = True,
    commit: bool = True,
) -> Tuple[bool, str, int]:
    is_valid, avatar_url, message = normalize_profile_avatar_url(raw_avatar_url)
    if not is_valid:
        return False, message, 400
    if avatar_url and _has_blocked_avatar_source(source):
        return False, "La fuente de imagen de perfil no tiene consentimiento valido.", 400
    normalized_source = _normalize_avatar_source(source)
    if avatar_url and not normalized_source:
        return False, "La fuente de imagen de perfil no es una fuente consentida.", 400

    metadata = dict(_profile_metadata(user))
    identity = dict(metadata.get("identity") if isinstance(metadata.get("identity"), dict) else {})
    existing_avatar = identity.get("avatar_url") or metadata.get("profile_avatar_url")
    existing_source = identity.get("avatar_source") or metadata.get("profile_avatar_source")

    if existing_avatar and not overwrite:
        existing_source_normalized = _normalize_avatar_source(existing_source)
        if existing_source_normalized in {"profile_upload", "profile_url"}:
            return True, "Avatar existente preservado.", 200

    if avatar_url:
        identity["avatar_url"] = avatar_url
        identity["avatar_source"] = normalized_source
        identity["avatar_consent"] = True
        identity["avatar_consented_at"] = datetime.utcnow().isoformat() + "Z"
    else:
        identity.pop("avatar_url", None)
        identity.pop("avatar_source", None)
        identity.pop("avatar_consent", None)
        identity.pop("avatar_consented_at", None)

    metadata["identity"] = identity
    user.accesibilidad = metadata
    flag_modified(user, "accesibilidad")

    if commit:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception(
                "[user_service] Error al guardar avatar de perfil para usuario %s",
                getattr(user, "id", None),
            )
            return False, "No se pudo actualizar la imagen de perfil.", 500

    return True, "Avatar actualizado correctamente.", 200


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
        avatar_key = next(
            (key for key in ("avatar_url", "picture", "profile_avatar_url") if key in data),
            None,
        )
        if avatar_key:
            avatar_consent_denied = _avatar_consent_denied(data.get("avatar_consent")) or _avatar_consent_denied(
                data.get("profile_picture_consent")
            )
            success, message, status = set_user_profile_avatar(
                user,
                "" if avatar_consent_denied else data.get(avatar_key),
                source=data.get("avatar_source") or "profile_url",
                overwrite=True,
                commit=False,
            )
            if not success:
                g.profile_update_error = (message, status)
                return False

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
