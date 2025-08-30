import requests
from flask import current_app

VERIFY_URL = "https://www.google.com/recaptcha/api/siteverify"

def verify_recaptcha(response_token: str) -> bool:
    """Verifica el token de reCAPTCHA con el servicio de Google."""
    secret = current_app.config.get("RECAPTCHA_SECRET_KEY")
    if not secret:
        # En entornos de desarrollo puede no estar configurada la clave,
        # en cuyo caso omitimos la verificación para evitar errores 400.
        current_app.logger.warning(
            "RECAPTCHA_SECRET_KEY no configurada; omitiendo verificación reCAPTCHA"
        )
        return True
    try:
        resp = requests.post(
            VERIFY_URL,
            data={"secret": secret, "response": response_token},
            timeout=5,
        )
        if resp.status_code != 200:
            current_app.logger.warning(
                "Fallo en la verificación de reCAPTCHA: HTTP %s", resp.status_code
            )
            return False
        result = resp.json()
        return result.get("success", False)
    except Exception as e:
        current_app.logger.error(f"Error al verificar reCAPTCHA: {e}")
        return False
