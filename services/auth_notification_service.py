import os
import smtplib
from email.message import EmailMessage
from typing import Optional
from urllib.parse import urlencode

from flask import current_app

from models import User


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def build_email_verification_url(user: User) -> Optional[str]:
    token = getattr(user, "email_verification_token", None)
    if not token:
        return None

    base_url = (
        os.getenv("AUTH_EMAIL_VERIFY_BASE_URL")
        or os.getenv("APP_BASE_URL")
        or current_app.config.get("APP_BASE_URL")
        or "https://www.chatboc.ar"
    )
    base_url = str(base_url).rstrip("/")
    return f"{base_url}/auth/verify-email?{urlencode({'token': token})}"


def _smtp_settings() -> dict:
    return {
        "host": os.getenv("ZOHO_SMTP_HOST") or os.getenv("SMTP_HOST") or "smtp.zoho.com",
        "port": int(os.getenv("ZOHO_SMTP_PORT") or os.getenv("SMTP_PORT") or "587"),
        "user": os.getenv("ZOHO_SMTP_USER") or os.getenv("SMTP_USER"),
        "password": os.getenv("ZOHO_SMTP_PASSWORD") or os.getenv("SMTP_PASSWORD"),
        "from_email": (
            os.getenv("AUTH_EMAIL_FROM")
            or os.getenv("ZOHO_SMTP_FROM")
            or os.getenv("SMTP_FROM")
            or "info@chatboc.ar"
        ),
        "use_tls": not _truthy(os.getenv("ZOHO_SMTP_DISABLE_TLS") or os.getenv("SMTP_DISABLE_TLS")),
    }


def send_verification_email(user: User, *, reason: str = "signup") -> bool:
    """Send the auth verification email through Zoho SMTP when configured.

    This deliberately never logs the raw token. Production logs must not become
    an account verification bypass.
    """

    verify_url = build_email_verification_url(user)
    if not verify_url:
        return False

    settings = _smtp_settings()
    if not settings["user"] or not settings["password"]:
        current_app.logger.warning(
            "[auth_email] Zoho SMTP not configured; verification email skipped for user_id=%s reason=%s",
            getattr(user, "id", None),
            reason,
        )
        return False

    msg = EmailMessage()
    msg["Subject"] = "Verifica tu cuenta de Chatboc"
    msg["From"] = settings["from_email"]
    msg["To"] = user.email
    msg.set_content(
        "\n".join(
            [
                f"Hola {user.name or ''}".strip() + ",",
                "",
                "Para terminar de activar tu cuenta de Chatboc, abri este enlace:",
                verify_url,
                "",
                "Si no creaste esta cuenta, podes ignorar este mensaje.",
                "",
                "Chatboc",
            ]
        )
    )
    msg.add_alternative(
        f"""
        <html>
          <body style="font-family:Arial,sans-serif;background:#f6f8fb;padding:24px;color:#111827;">
            <table role="presentation" width="100%" cellspacing="0" cellpadding="0"
                   style="max-width:560px;margin:auto;background:#ffffff;border-radius:16px;border:1px solid #e5e7eb;">
              <tr>
                <td style="padding:28px;">
                  <h1 style="font-size:22px;margin:0 0 12px;">Verifica tu cuenta</h1>
                  <p style="font-size:15px;line-height:1.6;margin:0 0 20px;">
                    Hola {user.name or ''}, activa tu acceso a Chatboc para continuar con tu panel, tenants y canales.
                  </p>
                  <a href="{verify_url}"
                     style="display:inline-block;background:#1455ff;color:#ffffff;text-decoration:none;
                            padding:12px 18px;border-radius:10px;font-weight:700;">
                    Verificar email
                  </a>
                  <p style="font-size:12px;line-height:1.5;color:#64748b;margin-top:22px;">
                    Si no creaste esta cuenta, podes ignorar este mensaje.
                  </p>
                </td>
              </tr>
            </table>
          </body>
        </html>
        """,
        subtype="html",
    )

    try:
        with smtplib.SMTP(settings["host"], settings["port"], timeout=12) as smtp:
            if settings["use_tls"]:
                smtp.starttls()
            smtp.login(settings["user"], settings["password"])
            smtp.send_message(msg)
        current_app.logger.info(
            "[auth_email] Verification email sent user_id=%s reason=%s",
            getattr(user, "id", None),
            reason,
        )
        return True
    except Exception as exc:  # pragma: no cover - external SMTP failure
        current_app.logger.warning(
            "[auth_email] Verification email failed user_id=%s reason=%s error=%s",
            getattr(user, "id", None),
            reason,
            exc,
            exc_info=True,
        )
        return False


def send_onboarding_whatsapp(user: User, *, tenant_name: str, tenant_slug: str) -> bool:
    phone = getattr(user, "telefono", None)
    if not phone:
        return False

    if _truthy(os.getenv("AUTH_WHATSAPP_ONBOARDING_DISABLED")):
        return False

    body = (
        f"Hola {user.name or ''}. Tu espacio Chatboc para {tenant_name} ya esta creado.\n"
        f"Tenant: {tenant_slug}\n"
        "Podes completar integraciones, WhatsApp y plantilla desde el panel."
    )
    try:
        from services.whatsapp_sender import send_whatsapp_message

        return bool(send_whatsapp_message(phone, body))
    except Exception as exc:  # pragma: no cover - external Twilio failure
        current_app.logger.warning(
            "[auth_whatsapp] Onboarding WhatsApp failed user_id=%s tenant=%s error=%s",
            getattr(user, "id", None),
            tenant_slug,
            exc,
            exc_info=True,
        )
        return False
