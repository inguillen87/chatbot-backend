# Frontend Tasks - Urgent Priority

This document outlines critical issues identified in the frontend application that require immediate attention to resolve user crashes, navigation errors, and UX regressions.

## 1. Critical Runtime Errors (Widget Crash)
**Priority: Highest**
The chat widget (`iframe.js`) is crashing due to undefined variables.

*   **Error:** `ReferenceError: applyVar is not defined`
*   **Location:** `iframe.js:41` (likely inside `Ys` or `jo` functions).
*   **Action:** Verify imports in the widget source code. Ensure `applyVar` is imported or defined before use.
*   **Error:** `ReferenceError: Badge is not defined`
*   **Action:** Ensure the `Badge` component is imported in the file rendering the widget UI.

## 2. Navigation & Routing (404 Errors)
**Priority: High**
The frontend is attempting to access routes that do not exist or are not handled correctly by the router.

*   **Missing Routes:**
    *   `/noticias/eventos`
    *   `/noticias/encuestas`
    *   `/municipio/reclamos/nuevo`
*   **Action:**
    *   Implement these pages in the frontend router (e.g., Next.js `pages/` or React Router).
    *   **OR** Update the links in the UI to point to the correct existing routes.

## 3. Landing Page "Agenda" Button
**Priority: Medium**
The "Agenda una Consultoría Personalizada" button incorrectly redirects to the generic `/contacto` page.

*   **Current Behavior:** Redirects to `https://www.chatboc.ar/contacto`.
*   **Required Behavior:** Redirect to an external scheduling tool (Calendly, Google Calendar) to book a meeting directly with the CEO.
*   **Action:** Update the `href` or `onClick` handler of the "Agenda" button on the landing page.

## 4. Chat Widget UX Improvements
**Priority: Medium**
*   **Message Flow:** Chat messages should flow **downwards** (newest at the bottom), similar to standard messaging apps (WhatsApp, Messenger). Currently, they may be appearing in reverse or scrolling incorrectly.
*   **Animations:** Ensure smooth entry animations for new messages.
*   **"Pip" Sound:** The backend has been updated to reduce system event noise, but the frontend should ensure it only plays the notification sound for *actual* new messages from the bot/agent, not for connection status updates.
*   **Tooltips:** User reported missing tooltips. Verify that `cta_messages` from the backend (which are now populated correctly) are being rendered as tooltips or suggestions.

## 5. Demo Dashboard & Hierarchy
**Priority: Medium**
*   **Rubros Hierarchy:** The backend now supports a nested tree structure for categories.
    *   **Action:** Ensure the frontend fetches `/api/rubros?format=tree` and renders a nested accordion/menu (Sector -> Sub-sector -> Demo) instead of a flat list ("chorizo").
*   **Demo Dashboards:**
    *   The backend now seeds real content (News, Events, Surveys) for demo tenants (`municipio`, `bodega`, `ferreteria`).
    *   **Action:** Ensure the `/demo/<slug>` pages fetch and display this content (`/api/public/news`, `/api/public/events`, `/api/public/surveys`) to show a populated dashboard instead of an empty page.

## 6. Cart Persistence
**Priority: Medium**
*   **Issue:** User reports cart resets to zero.
*   **Backend Status:** The backend correctly handles `X-Anon-Id` for persistence.
*   **Action:** Ensure the frontend generates a stable `anon_id`, stores it in `localStorage`, and sends it in the `X-Anon-Id` header for **every** request to the cart API (`/api/pwa/public/cart/...`).
