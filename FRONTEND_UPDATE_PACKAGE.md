# Frontend Update Package - Version 2024.10.27

## Overview
This update introduces a new hierarchical structure for selecting business categories (rubros) and refactors the chat interaction for demos to be more interactive and less verbose.

## 1. Rubros Selection Hierarchy (New Structure)

The API response for fetching rubros (likely via `/api/rubros`) will now reflect a nested structure. The frontend must be updated to render this hierarchy, preferably using an accordion or nested list UI.

### New Hierarchy
*   **Municipios y Gobierno**
    *   *Municipios* (e.g., Junín Demo)
*   **Locales Comerciales**
    *   **Alimentación y Bebidas**
        *   Almacenes
        *   Bodegas y Vinos
        *   Kioscos
        *   Gastronomía
    *   **Retail y Comercios**
        *   Ferretería y Construcción
        *   Indumentaria y Moda
    *   **Servicios Profesionales**
        *   Logística y Transporte
        *   Seguros y Riesgos
        *   Fintech y Banca
        *   Inmobiliaria y Real Estate
        *   Energía e Industria
    *   **Salud y Bienestar**
        *   Salud y Medicina (Médico General)
        *   Farmacias
    *   **Producción e Industria**
        *   (Future expansion)

### Frontend Implementation Guide
1.  **Fetch Data:** Ensure the frontend fetches the rubros list. The backend `Rubro` model has a `padre_id` field.
2.  **Grouping:** Group rubros by their `padre_id`.
    *   Root items: `padre_id` is null (or check for specific root keys `municipios_root`, `comerciales_root`).
    *   Level 1 items: `padre_id` points to a Root item.
    *   Level 2 items (Demos): `padre_id` points to a Level 1 item.
3.  **UI Component:**
    *   Display Root categories as main headers (tabs or large sections).
    *   Display Level 1 categories as collapsible sections (accordions) or grid headers.
    *   Display Level 2 items as clickable cards/buttons that launch the demo.

## 2. Interactive Demo Flow (Chat Widget)

The "Welcome" experience for Demos has been refactored to prevent large text dumps ("chorizos").

### Changes
*   **Structured Menus:** The backend now sends `menu_sections` in the response payload.
*   **Interactive Lists:** The frontend should render `interactive_list` or `interactive_buttons` message types.
*   **No Text Dump:** The backend no longer appends the entire menu tree as plain text to the `message_body`.

### Frontend Requirements
1.  **Render `menu_sections`:** If the JSON response contains `menu_sections`, render them as distinct UI blocks (e.g., a carousel of options, a list of buttons, or a native-like menu).
2.  **Handle `interactive_list`:** Ensure the chat widget supports the `interactive_list` type, which presents a button that opens a selection modal/drawer.
3.  **Visual Polish:**
    *   Use the `emoji` field provided in quick actions to add visual cues.
    *   Display `description` text in menu items as a subtitle (smaller font).

## 3. Specific Demo Improvements

### Logística Demo
*   **Config Updated:** The `welcome_message` is now concise.
*   **Flow:** Users are invited to click buttons rather than read a wall of text.

## 4. Pending Tasks / Verification
*   **Check "Undefined":** Ensure that all demos have a valid `nombre` and `welcome_title` in their configuration to avoid "undefined" headers in the widget.
*   **Images:** Verify that demo resources (PDFs, images) are accessible via the `/media` or `/static` paths provided in the JSON config.
