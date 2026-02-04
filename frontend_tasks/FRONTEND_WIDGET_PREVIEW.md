# Widget Preview & Behavior Guidelines

To match the backend improvements, the frontend widget (loader + iframe) should handle the following:

## 1. Loader Script (widget.js)
-   **Async Loading**: Ensure the script doesn't block page load.
-   **Tenant Resolution**: Pass `tenant_slug` or ID cleanly to the iframe URL.
-   **Iframe Resizing**: Listen to `postMessage` events from the iframe to resize (open vs closed state).

## 2. Iframe App (The actual chat)
-   **Theme Application**:
    -   Read `theme_config` or `colors` from the API response (`GET /widget-config`).
    -   Apply CSS variables: `--primary-color`, `--secondary-color`, `--font-family`.
-   **Mobile Experience**:
    -   On mobile, when open, the iframe should take 100% viewport (width/height).
    -   Add a "Close" button clearly visible on mobile header.
-   **Socket.IO Connection**:
    -   Connect to `/api/socket.io`.
    -   Join room `pyme_{id}` or specific chat room.
    -   Handle `message_received` event to show unread badge or play sound.

## 3. Preview Mode (Admin Panel)
-   When rendering the widget inside the Admin Panel (for preview), pass a flag (e.g., `?mode=preview`) to disable analytics tracking or actual socket connections if preferred, or just to mock the "Open" state.
