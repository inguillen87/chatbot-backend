import logging
import uuid
import os
from importlib import import_module

from models import User, db
from services.user_service import set_user_profile_avatar


def _load_google_auth_module(module_name: str):
    """Load google-auth only when token verification actually runs."""

    try:
        return import_module(module_name)
    except Exception as exc:  # pragma: no cover - depends on optional package
        raise ImportError("google-auth missing") from exc


class _DeferredGoogleRequest:
    """Patch-friendly request proxy that keeps google-auth out of startup."""

    def __init__(self, *args, **kwargs):
        self._args = args
        self._kwargs = kwargs
        self._delegate = None

    def _load(self):
        if self._delegate is None:
            requests_module = _load_google_auth_module(
                "google.auth.transport.requests"
            )
            self._delegate = requests_module.Request(*self._args, **self._kwargs)
        return self._delegate

    def __call__(self, *args, **kwargs):
        return self._load()(*args, **kwargs)


class _GoogleRequestsFacade:
    Request = _DeferredGoogleRequest


class _GoogleIdTokenFacade:
    def verify_oauth2_token(self, *args, **kwargs):
        provider = _load_google_auth_module("google.oauth2.id_token")
        return provider.verify_oauth2_token(*args, **kwargs)


# Keep these module-level seams stable: login tests and integrations patch them.
id_token = _GoogleIdTokenFacade()
google_requests = _GoogleRequestsFacade()

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
            acepto_terminos=False,
            fecha_aceptacion_terminos=None,
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
