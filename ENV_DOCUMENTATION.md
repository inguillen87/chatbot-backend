# Environment Variables Documentation

New variables introduced for Multi-tenant Modules:

## Integrations
*   `TIENDANUBE_CLIENT_ID`: Client ID for TiendaNube OAuth app.
*   `TIENDANUBE_CLIENT_SECRET`: Secret for validating TiendaNube webhooks/OAuth.
*   `MERCADOLIBRE_APP_ID`: App ID for MercadoLibre integration.
*   `MERCADOLIBRE_CLIENT_SECRET`: Secret for validating MercadoLibre webhooks/OAuth.
*   `MERCADOPAGO_ACCESS_TOKEN`: (Existing) Global access token, overridden by tenant configuration if present.

## Notifications
*   `TELEGRAM_BOT_TOKEN`: Token for the Telegram bot used to notify owners.
*   `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`: (Existing) Used for SMS.
*   `TWILIO_WHATSAPP_NUMBER`: (Existing) Used for WhatsApp notifications.
*   `TWILIO_META_APP_ID`, `TWILIO_META_EMBEDDED_SIGNUP_CONFIG_ID`, `TWILIO_PARTNER_SOLUTION_ID`: Meta/Twilio Tech Provider identifiers used by the backend-driven WhatsApp onboarding contract.
*   `TWILIO_TECH_PROVIDER_LIVE_ENABLED`: Enables live Twilio API calls for subaccounts, Messaging Services, WhatsApp Senders and Voice TwiML Apps.
*   `TWILIO_TENANT_AUTO_BOOTSTRAP_ENABLED`: Seeds every new tenant with a WhatsApp onboarding contract. Defaults to true.
*   `TWILIO_TENANT_AUTO_PROVISION_ENABLED`: When true, new tenants automatically run the Twilio provisioning step instead of only storing a plan.
*   `RENDER_ENV_SYNC_ENABLED`, `RENDER_API_KEY`, `RENDER_SERVICE_ID` or `RENDER_ENV_GROUP_ID`: Optional secret-store sync so generated `TWILIO_SUBACCOUNT_AUTH_TOKEN_<ACCOUNT_SID>` values are pushed to Render instead of handled manually.
*   `RENDER_ENV_SYNC_TRIGGER_DEPLOY_ENABLED`: Optional follow-up deploy trigger after Render env sync so the backend reloads the newly generated subaccount token.

## General
*   `ADMIN_EMAIL`: (Existing) Fallback email for admin notifications.
*   `APP_BASE_URL`: Base URL for links generated in emails (e.g., `https://chatboc.ar`).

## AI providers
*   `OPENAI_API_KEY`: OpenAI API key used by chat, vision, audio and realtime flows.
*   `OPENAI_CHAT_MODEL_DEFAULT`: Default OpenAI chat model. Current backend default is `gpt-4o-mini`.
*   `COHERE_API_KEY`: Cohere API key used only as LLM fallback.
*   `COHERE_CHAT_MODEL`: Cohere chat fallback model. Default is `command-a-03-2025`; the old `command-r` alias is deprecated.
*   `COHERE_CHAT_API_VERSION`: Cohere chat API version. Default is `v2`, with `v1` retained for compatibility.
