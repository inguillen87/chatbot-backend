# Frontend UX & UI Guidelines for Chatboc

This document outlines the changes required in the Frontend application to support the new "Clean UI" initiatives, specifically for Category Navigation and Chat Interactions.

## 1. Rubros (Categories) Hierarchy

The backend now returns a strict, 3-level hierarchy for categories to clean up the "Rubros" landing page.

*   **Root Categories (Level 1):**
    *   `Gobierno y Municipios`
    *   `Comercio y Retail`
    *   `Servicios y Profesionales`

*   **Sub Categories (Level 2):**
    *   Mapped specifically to roots (e.g., "Almacén" -> "Comercio", "Logística" -> "Servicios").

### Frontend Rendering Logic

When fetching `/api/rubros`, you will receive a flat list containing `padre_id`. You **must** construct the tree client-side or use the `padre_id` to group them visually.

**Do Not:**
*   Display a flat list of all categories.
*   Mix Government and Commercial options indiscriminately.

**Do:**
*   Create 3 distinct columns or tabs based on the Root Categories.
*   Filter the list: `rubros.filter(r => r.padre_id === ROOT_ID)`.

## 2. Chat Interaction & Menus

We have removed the "Text Wall" (unintelligible list of options in the message body) from the backend response. The backend now prioritizes structured data.

### Payload Structure

The `POST /ask` response will now contain:

```json
{
  "message_body": "Brief intro text only.",
  "options_list": [ ... buttons ... ],
  "menu_sections": [
     {
        "title": "Main Menu",
        "items": [ ... ]
     }
  ]
}
```

### Rendering Rules

1.  **Prioritize Buttons:** If `options_list` is present, display them as chips or buttons immediately below the `message_body`.
2.  **Hide Text Menus:** Do not rely on parsing the text body for options. The text body will now be very short (e.g., "Select an option below").
3.  **Interactive Lists:** If `menu_sections` is present, render a "Menu" button that opens a bottom-sheet (mobile) or dropdown (desktop) with the structured sections.

## 3. Demo Customization

Each demo (Municipality vs. Winery vs. Logistics) has specific metadata:
*   **Vouchers/Benefits:** Display specific UI components for Govt demos.
*   **Products:** Display Product Cards for Retail demos.

Use the `demo_metadata` field in the chat response to toggle these UI modes.
