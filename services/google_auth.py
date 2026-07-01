import logging
import uuid
import os
from datetime import datetime
try:  # pragma: no cover - puede faltar google-auth en tests
    from google.oauth2 import id_token
    from google.auth.transport import requests as google_requests
except Exception:  # pragma: no cover - define stubs
    from types import SimpleNamespace
    id_token = SimpleNamespace(verify_oauth2_token=lambda *a, **k: (_ for _ in ()).throw(ImportError("google-auth missing")))
    google_requests = SimpleNamespace(Request=object)
from models import User, db
from services.user_service import set_user_profile_avatar

ALLOWED_CLIENT_IDS = []
env_ids = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
if env_ids:
    ALLOWED_CLIENT_IDS = [i.strip() for i in env_ids.split(',') if i.strip()]

logger = logging.getLogger(__name__)

def _normalizar_tipo(tipo: str | None) -> str | None:
    if not tipo:
        return None
    sinonimos = {
        "muni": "municipio",
        "municipios": "municipio",
        "municipio": "municipio",
        "pymes": "pyme",
        "pyme": "pyme",
    }
    return sinonimos.get(str(tipo).strip().lower())

def login_o_crear_usuario(token_id: str, *, rol: str | None = None, tipo_chat: str | None = None) -> User:
    """Verifica el token de Google y devuelve un usuario existente o nuevo.

    El/los ID de cliente permitidos se establecen con la variable de entorno
    ``GOOGLE_OAUTH_CLIENT_ID``. Se pueden separar múltiples IDs con coma.
    ``rol`` y ``tipo_chat`` se aplican sólo cuando se crea un usuario nuevo.
    """
    logger.info(f"Attempting Google login with token_id: {token_id[:15]}...")
    logger.info(f"Allowed client IDs: {ALLOWED_CLIENT_IDS}")
    try:
        info = id_token.verify_oauth2_token(token_id, google_requests.Request())
        logger.info(f"Token verified. Info: {info}")
        if ALLOWED_CLIENT_IDS and info.get('aud') not in ALLOWED_CLIENT_IDS:
            logger.error(f"Unauthorized audience. aud: {info.get('aud')}")
            raise ValueError('audiencia no autorizada')
        email = info.get('email')
        name = info.get('name') or (email.split('@')[0] if email else 'Usuario')
    except ValueError as e:
        logger.error(f"ValueError while verifying token: {e}", exc_info=True)
        raise
    except Exception as e:  # pragma: no cover - requiere internet
        logger.error(f"Error verificando token de Google: {e}", exc_info=True)
        raise ValueError("Token de Google inválido")

    if not email:
        raise ValueError("Token de Google sin email")

    user = User.query.filter_by(email=email.lower()).first()
    created = False
    if not user:
        logger.info(f"Creating new user with Google info: {info}")
        tipo_normalizado = _normalizar_tipo(tipo_chat) or "pyme"
        rol_final = rol if rol in {"admin", "usuario"} else "usuario"
        user = User(
            name=name.strip(),
            email=email.lower(),
            token=str(uuid.uuid4()),
            nombre_empresa="",
            rubro_id=None,
            plan="gratis",
            rol=rol_final,
            tipo_chat=tipo_normalizado,
            acepto_terminos=True,
            fecha_aceptacion_terminos=datetime.utcnow(),
        )
        user.set_password(str(uuid.uuid4()))
        db.session.add(user)
        created = True

    picture_url = info.get("picture")
    if picture_url:
        set_user_profile_avatar(
            user,
            picture_url,
            source="google",
            overwrite=False,
            commit=False,
        )

    if created or picture_url:
        db.session.commit()
    return user
