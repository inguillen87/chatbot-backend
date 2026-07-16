from database import db
from utils.auth_helpers import auth_session_version, generar_token


AUTHORIZED_SUPERADMIN_EMAIL = "guillen.marce@gmail.com"


def clerk_superadmin_headers(user):
    """Issue the same constrained session shape used after a Clerk exchange."""

    user.email = AUTHORIZED_SUPERADMIN_EMAIL
    db.session.add(user)
    db.session.commit()
    sid = f"test-clerk-session-{user.id}"
    token = generar_token(
        user.id,
        user.rol,
        user.tipo_chat,
        user.municipio_id,
        user.pyme_id,
        extra_claims={
            "auth_provider": "clerk",
            "session_kind": "clerk",
            "clerk_sid": sid,
            "clerk_user_id": f"user_test_{user.id}",
            "sid": sid,
            "jti": f"test-jti-{user.id}",
            "sv": auth_session_version(user),
        },
    )
    return {"Authorization": f"Bearer {token}"}
