# Frontend Catalog & Widget Tasks

## 1. Widget Sizing & Styling
- [ ] **Fix Widget Scale:** The widget container (`#chatboc-widget-container`) currently has a CSS injection (`transform: scale(1.05)`) applied from the backend (`/api/public/tenants/.../widget-config`) to improve readability. This should be properly handled in the frontend CSS/JS to avoid reliance on backend overrides and ensure responsiveness on mobile (where scale should be 1).
- [ ] **Chat Window Opening:** Users report that "Hablar con un agente" does not automatically open the chat window if closed. Ensure the widget listens for trigger events or the backend `pedir_info` signals to force-open the chat UI.

## 2. Integration Connection Feedback
- [ ] **Handle 422 Unprocessable Entity:** The integration connect endpoints (`/api/admin/tenants/.../integrations/.../connect`) now return `422` with a specific JSON error message (e.g., `{"error": "platform_not_configured", "message": "..."}`) when server-side keys are missing.
    - **Task:** Update the Admin UI to catch 422 errors and display the `message` field to the user in a toast/alert, instead of generic "Server Error".

## 3. Order Management UI
- [ ] **Orders Not Appearing:** Users reported created orders are not visible in the Admin Tenant > Orders section.
    - **Task:** Verify the API call used to fetch orders (`GET /api/admin/tenants/<slug>/orders` vs `/api/orders`). Ensure the frontend is sending the correct `tenant_slug` or `tenant_id` query parameters if required by the backend filters.
    - **Task:** Check if the frontend properly handles pagination or status filters that might be hiding "Pending" or "Open" orders by default.

## 4. Chat & Agent Interaction
- [ ] **Live Chat Trigger:** When the backend returns a "Derivar a Humano" response (often accompanied by a `ticket_id` in the payload), the frontend should visually indicate a handover status or switch the chat mode if applicable.
- [ ] **Audio Responses:** Ensure the widget properly renders and plays `audio_url` fields returned in the JSON response, specifically for "Neutral Argentine" voice responses.

## 5. Catalog Display
- [ ] **Rich Response Rendering:** The backend sends `message_type: "interactive_list"` or `"interactive_buttons"` with an `options_list`. Ensure the frontend renders these as clickable elements.
- [ ] **Product Cards:** When `data.cart_summary` or `data.catalogo` items are present, render them as structured cards rather than just relying on the markdown `message_body`.

## 6. Catalog Management (Integrations)
- [ ] **Integrations > Catalog Section:** Add a dedicated section under Integrations to manage the catalog **without consuming AI tokens**. This should allow:
  - Uploading/replacing the full catalog file.
  - Editing cells (price, name, description, stock).
  - Adding/removing rows (new articles).
  - Validating required fields before saving.
- [ ] **Tenant Catalog CDN Link:** Display and copy a per-tenant CDN URL where the catalog is hosted (for sharing via WhatsApp, widget chat, email, etc.). Include:
  - A “Copy link” button.
  - A short description/banner preview (if available).
  - Guidance text that this link is used for end-user catalog browsing outside the AI flow.
- [ ] **Widget/WhatsApp Sharing CTA:** Add a small CTA in the catalog section to generate/share the catalog link (button to “Enviar por WhatsApp” or “Compartir enlace”) that uses the CDN URL.
