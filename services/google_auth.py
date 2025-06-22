import logging
import uuid
from datetime import datetime
try:  # pragma: no cover - puede faltar google-auth en tests
    from google.oauth2 import id_token
    from google.auth.transport import requests as google_requests
except Exception:  # pragma: no cover - define stubs
    from types import SimpleNamespace
    id_token = SimpleNamespace(verify_oauth2_token=lambda *a, **k: (_ for _ in ()).throw(ImportError("google-auth missing")))
    google_requests = SimpleNamespace(Request=object)
from models import User, db

logger = logging.getLogger(__name__)

def login_o_crear_usuario(token_id: str) -> User:
    """Verifica el token de Google y devuelve un usuario existente o nuevo."""
    try:
        info = id_token.verify_oauth2_token(token_id, google_requests.Request())
        email = info.get('email')
        name = info.get('name') or (email.split('@')[0] if email else 'Usuario')
    except Exception as e:  # pragma: no cover - requiere internet
        logger.error(f"Error verificando token de Google: {e}")
        raise ValueError("Token de Google inválido")

    if not email:
        raise ValueError("Token de Google sin email")

    user = User.query.filter_by(email=email.lower()).first()
    if not user:
        user = User(
            name=name.strip(),
            email=email.lower(),
            token=str(uuid.uuid4()),
            nombre_empresa="",
            rubro_id=None,
            plan="gratis",
            rol="usuario",
            acepto_terminos=True,
            fecha_aceptacion_terminos=datetime.utcnow(),
        )
        user.set_password(str(uuid.uuid4()))
        db.session.add(user)
        db.session.commit()
    return user
