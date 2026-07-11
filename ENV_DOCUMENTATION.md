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
*   `ZOHO_SMTP_HOST`: SMTP host for transactional auth email. Defaults to `smtp.zoho.com`.
*   `ZOHO_SMTP_PORT`: SMTP port for transactional auth email. Defaults to `587`.
*   `ZOHO_SMTP_USER`: Zoho mailbox user, e.g. `info@chatboc.ar`.
*   `ZOHO_SMTP_PASSWORD`: Zoho mailbox/app password. Keep this server-side only.
*   `AUTH_EMAIL_FROM`: Sender email for verification messages. Defaults to `info@chatboc.ar`.
*   `AUTH_EMAIL_VERIFY_BASE_URL`: Base URL used for verification links. Defaults to `APP_BASE_URL`.
*   `AUTH_WHATSAPP_ONBOARDING_DISABLED`: Set to true to disable owner WhatsApp onboarding notifications.

## WhatsApp accessibility audio cache / Cloudflare
*   `TTS_CACHE_ENABLED`: Enables reusable MP3 cache for generated TTS audio. Defaults to true.
*   `WHATSAPP_MENU_AUDIO_ENABLED`: Enables audio alternatives for fixed WhatsApp menus. Defaults to true.
*   `TTS_AUDIO_CACHE_PUBLIC_BASE_URL`: Optional public CDN base URL for cached menu audios. When set, `static/audio_cache/*.mp3` links are returned from this base instead of `BACKEND_URL`.
*   `CLOUDFLARE_AUDIO_CACHE_PUBLIC_BASE_URL`: Alias for `TTS_AUDIO_CACHE_PUBLIC_BASE_URL` when the CDN is managed in Cloudflare.
*   `R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME`, `R2_PUBLIC_BASE_URL`, `R2_REGION`: Optional Cloudflare R2 configuration for uploaded assets. Audio uploads are marked with long-lived cache headers and should never include spoken text or PII in object keys.
*   `R2_SIGNED_URL_TTL_SECONDS`: Lifetime for private R2 download URLs generated for authenticated claim/ticket attachments. Defaults to `900` and is clamped between 60 and 3600 seconds.
*   `CLOUDFLARE_TURNSTILE_SECRET_KEY`: Server-side Turnstile secret for anonymous marketplace/widget intake validation. Keep it only in backend/Render.
*   `CLOUDFLARE_TURNSTILE_ENFORCE_PUBLIC_INTAKE`: When true, anonymous `/api/pedidos/from-file` requests from marketplace/widget/web must include a valid Turnstile token. Defaults to false so existing flows keep working until the frontend site key is enabled. If this is true but `CLOUDFLARE_TURNSTILE_SECRET_KEY` is missing, public intake fails closed instead of silently bypassing security.
    *   Production requires a real Cloudflare Turnstile widget configured for `chatboc.ar` and tenant marketplace domains. Frontend uses `VITE_CLOUDFLARE_TURNSTILE_SITE_KEY`; backend uses this secret and validates the token with Siteverify.
    *   Cloudflare dummy keys may be used only in local/dev smoke tests. Use dummy site key `1x00000000000000000000AA` with dummy secret `1x0000000000000000000000000000000AA` for an always-pass validation pair.

## Auth / Identity
*   `CLERK_ENABLED`: Enables Clerk session sync and tenant onboarding endpoints.
*   `VITE_CLERK_PUBLISHABLE_KEY`: Public Clerk key used by the Vite frontend. Safe to expose in the browser.
*   `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`: Public Clerk key used by Next.js frontends. Safe to expose in the browser.
*   `CLERK_SECRET_KEY`: Server-side Clerk key for admin/API operations when needed. Do not expose it to the frontend.
*   `CLERK_ISSUER`: Expected issuer for Clerk session JWT verification. Required unless `CLERK_JWKS_URL` is set.
*   `CLERK_JWKS_URL`: Optional explicit JWKS URL for Clerk session JWT verification.
*   `CLERK_AUDIENCE`: Optional JWT audience check for Clerk session tokens.
*   `CLERK_WEBHOOK_SIGNING_SECRET`: Clerk webhook signing secret used by `/auth/clerk/webhook`. `CLERK_WEBHOOK_SECRET` remains supported as a legacy alias.
    Subscribe the endpoint to `user.created`, `user.updated`, `user.deleted`, `session.ended`, `session.removed`, and `session.revoked`; terminal session events revoke Chatboc JWTs.
*   `CLERK_AUTHORIZED_PARTIES`: comma-separated frontend origins accepted from the signed Clerk `azp` claim. Production: `https://chatboc.ar,https://www.chatboc.ar`.
*   `CLERK_REQUIRE_AZP`: when `true`, reject session tokens without an authorized-party claim.
*   `CLERK_SUPERADMIN_EMAILS`: verified Clerk emails allowed to receive `super_admin`; keep this list explicit and minimal.
*   `CHATBOC_TERMS_VERSION`: version recorded when a tenant owner accepts the platform terms during onboarding.
*   `CLERK_WEBHOOK_TOLERANCE_SECONDS`: Allowed webhook timestamp drift. Defaults to `300`.
*   `CLERK_SOCIAL_PROVIDERS`: Comma-separated enabled providers for the frontend contract. Defaults to `google,facebook,linkedin`.
*   `LIVE_CHAT_ROOM_TOKEN_TTL_SECONDS`: Signed public ticket-room token lifetime. Defaults to `900` seconds and is capped at `3600`.
*   `TRACKING_FAILURE_RATE_LIMIT_ATTEMPTS`: Failed PIN attempts per IP and ticket in the configured window. Defaults to `5`.
*   `TRACKING_SUBJECT_FAILURE_RATE_LIMIT_ATTEMPTS`: Failed attempts per ticket across all IPs. Defaults to `20`.
*   `TRACKING_FAILURE_RATE_LIMIT_WINDOW_SECONDS`: Tracking failure window. Defaults to `60` seconds.

## General
*   `ADMIN_EMAIL`: (Existing) Fallback email for admin notifications.
*   `APP_BASE_URL`: Base URL for links generated in emails (e.g., `https://chatboc.ar`).

## AI providers
*   `OPENAI_API_KEY`: OpenAI API key used by chat, vision, audio and realtime flows.
*   `OPENAI_CHAT_MODEL_DEFAULT`: Default OpenAI chat model. Current backend default is `gpt-4o-mini`.
*   `LLM_PROVIDER_ORDER`: Ordered comma-separated LLM providers for chat orchestration. Default is `openai`. Example: `openai,gemini,ollama,cohere`.
    *   The orchestrator now also applies task-aware routing. If `task_type` or `usuario.ai_task_type` is `survey_insights`, `crm_summary`, `ticket_summary`, `analytics`, `insights`, `report`, `backoffice` or `batch_classification`, Ollama is promoted to first place when enabled. If the task is transactional or realtime (`whatsapp_realtime`, `reclamo`, `pedido`, `checkout`, `live_chat`, `voice`, etc.), Ollama is removed from that call unless the code explicitly opts into a different task.
*   `GEMINI_API_KEY`: Server-side Gemini API key. Keep it out of frontend `VITE_` variables and git-tracked files.
*   `GEMINI_CHAT_MODEL`: Gemini chat fallback model used when `gemini` is included in `LLM_PROVIDER_ORDER`. Default is `gemini-2.5-flash`.
*   `OLLAMA_ENABLED`: Enables the Ollama/OpenAI-compatible provider for experimental open-source/cloud models. Default is false.
*   `OLLAMA_BASE_URL`: OpenAI-compatible Ollama endpoint. Local default is `http://localhost:11434/v1`; Ollama Cloud can use its hosted OpenAI-compatible endpoint.
*   `OLLAMA_API_KEY`: Optional Ollama Cloud API key. Local Ollama does not require a real secret.
*   `OLLAMA_CHAT_MODEL`: Ollama model used when `ollama` is included in `LLM_PROVIDER_ORDER`. Default is `glm-5.2:cloud`.
*   `OLLAMA_TIMEOUT_SECONDS`: Timeout for Ollama calls. Default is `45`.
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
