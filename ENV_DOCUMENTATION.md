# Environment Variables Documentation

New variables introduced for Multi-tenant Modules:

## Integrations
*   `TIENDANUBE_CLIENT_ID`: Client ID for TiendaNube OAuth app.
*   `MERCADOLIBRE_APP_ID`: App ID for MercadoLibre integration.
*   `MERCADOPAGO_ACCESS_TOKEN`: (Existing) Global access token, overridden by tenant configuration if present.

## Notifications
*   `TELEGRAM_BOT_TOKEN`: Token for the Telegram bot used to notify owners.
*   `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`: (Existing) Used for SMS.
*   `TWILIO_WHATSAPP_NUMBER`: (Existing) Used for WhatsApp notifications.

## General
*   `ADMIN_EMAIL`: (Existing) Fallback email for admin notifications.
*   `APP_BASE_URL`: Base URL for links generated in emails (e.g., `https://chatboc.ar`).
