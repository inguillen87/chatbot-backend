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
*   `LLM_PROVIDER_ORDER`: Ordered comma-separated LLM providers for chat orchestration. Default is `openai`. Example: `openai,gemini,cohere`.
*   `GEMINI_API_KEY`: Server-side Gemini API key. Keep it out of frontend `VITE_` variables and git-tracked files.
*   `GEMINI_CHAT_MODEL`: Gemini chat fallback model used when `gemini` is included in `LLM_PROVIDER_ORDER`. Default is `gemini-2.5-flash`.
*   `COHERE_API_KEY`: Cohere API key used only as LLM fallback.
*   `COHERE_CHAT_MODEL`: Cohere chat fallback model. Default is `command-a-03-2025`; the old `command-r` alias is deprecated.
*   `COHERE_CHAT_API_VERSION`: Cohere chat API version. Default is `v2`, with `v1` retained for compatibility.
*   `HUGGINGFACE_API_TOKEN`: Optional Hugging Face token for task-specific inference services, not enabled as a core chat provider by default.
*   `HUGGINGFACE_ENABLED`: Enables future Hugging Face task integrations when set to true. Default is false.
*   `HUGGINGFACE_PROVIDER`: Inference Providers routing mode. Default is `auto`.
*   `HUGGINGFACE_EMBEDDINGS_ENABLED`: Enables Hugging Face embeddings as an OpenAI fallback. The backend rejects vectors that do not match the configured Qdrant dimension.
*   `HUGGINGFACE_EMBEDDING_MODEL`: Embedding model for catalog/search fallback. Default is `intfloat/multilingual-e5-large`.
*   `HUGGINGFACE_ZERO_SHOT_ENABLED`: Enables zero-shot classification helpers for future category/priority automation.
*   `HUGGINGFACE_ZERO_SHOT_MODEL`: Multilingual zero-shot model. Default is `joeddav/xlm-roberta-large-xnli`.
*   `HUGGINGFACE_RECLAMO_CATEGORY_MIN_SCORE`: Minimum zero-shot score required before using a Hugging Face complaint category suggestion. Default is `0.72`.
*   `HUGGINGFACE_RECLAMO_PRIORITY_MIN_SCORE`: Minimum zero-shot score required before storing a Hugging Face complaint priority suggestion. Default is `0.66`.
*   `HUGGINGFACE_RECLAMO_SIGNAL_MIN_SCORE`: Minimum zero-shot score for municipal operational signals such as risk, evidence needed, or exact-location needed. Default is `0.62`.
*   `HUGGINGFACE_SENTIMENT_MIN_SCORE`: Minimum zero-shot score for citizen/customer sentiment hints. Default is `0.56`.
*   `HUGGINGFACE_PYME_INTENT_MIN_SCORE`: Minimum zero-shot score for business ticket intent hints such as order, payment, delivery, support, or human handoff. Default is `0.62`.
*   `VISION_HUGGINGFACE_ENABLED`: Enables Hugging Face image classification/object detection as a final vision fallback.
*   `HUGGINGFACE_IMAGE_CLASSIFICATION_MODEL`: Image classification model. Default is `google/vit-base-patch16-224`.
*   `HUGGINGFACE_OBJECT_DETECTION_MODEL`: Object detection model. Default is `facebook/detr-resnet-50`.
*   `INSTALL_OPEN_SOURCE_AI_EXTRAS`: Installs optional open-source AI packages from `requirements-ai-oss.txt` during build. Default is false.
*   `DOCLING_ENABLED`: Enables optional Docling document conversion for catalog/document uploads before LLM vision fallback. Default is false.
*   `DOCLING_MAX_FILE_MB`: Maximum file size processed by Docling. Default is 15 MB.
