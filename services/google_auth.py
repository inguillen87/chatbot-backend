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
    ``tipo_chat`` se aplica sólo cuando se crea un usuario nuevo. ``rol`` se
    conserva en la firma por compatibilidad, pero nunca se confía en un rol
    enviado por el cliente: toda alta social comienza con privilegio mínimo.
    """
    if not ALLOWED_CLIENT_IDS:
        logger.error("Google login rejected because no OAuth audience is configured")
        raise ValueError("Google OAuth no configurado")

    logger.info("Attempting Google login")
    try:
        info = id_token.verify_oauth2_token(token_id, google_requests.Request())
        if not isinstance(info, dict):
            raise ValueError("token payload invalid")
        if info.get('aud') not in ALLOWED_CLIENT_IDS:
            logger.warning("Google token rejected because its audience is not allowed")
            raise ValueError('audiencia no autorizada')
        if info.get('email_verified') is not True:
            logger.warning("Google token rejected because its email is not verified")
            raise ValueError('email de Google no verificado')
        email = info.get('email')
        name = info.get('name') or (email.split('@')[0] if email else 'Usuario')
        logger.info("Google token verified")
    except ValueError:
        logger.warning("Google token rejected by identity policy")
        raise
    except Exception:  # pragma: no cover - requiere internet
        # Provider exceptions can include claim details. Keep authentication
        # logs useful without persisting tokens or identity payloads.
        logger.error("Google token verification failed")
        raise ValueError("Token de Google inválido")

    if not email:
        raise ValueError("Token de Google sin email")

    user = User.query.filter_by(email=email.lower()).first()
    created = False
    if not user:
        logger.info("Creating a new least-privilege user from verified Google identity")
        tipo_normalizado = _normalizar_tipo(tipo_chat) or "pyme"
        user = User(
            name=name.strip(),
            email=email.lower(),
            token=str(uuid.uuid4()),
            nombre_empresa="",
            rubro_id=None,
            plan="gratis",
            rol="usuario",
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
