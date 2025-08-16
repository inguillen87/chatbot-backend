from models import db, User
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
