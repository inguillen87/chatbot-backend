# Frontend Catalog Tasks & Integration Guide

## Overview
We are implementing a robust, industry-specific catalog system. The backend now supports a 2-step upload process (Upload -> Preview -> Confirm) and industry-specific processors (Winery, Construction, Fashion, etc.).

## 1. Catalog Upload Widget (New Stepper UI)

**Goal:** Replace the simple file input with a multi-step wizard.

### Step 1: File Selection
*   **UI:** Drag & Drop zone + "Or paste URL" input.
*   **Action:** POST to `/api/catalog/upload`.
    *   Payload (Multipart): `file` (binary) OR JSON `{"file_url": "https://..."}`.
*   **Loading State:** Show "Analyzing file..." (The backend runs OCR/Parsing immediately).

### Step 2: Preview & Mapping (The "Smart" Step)
*   **Input Data:** The JSON response from Step 1.
    *   `status`: `preview_ready` or `needs_mapping`.
    *   `preview_items`: Array of first ~5 items detected.
    *   `detected_columns`: List of headers found in the file (e.g., ["Producto", "Precio", "Vino"]).
    *   `expected_fields`: List of fields the backend needs (e.g., `["sku", "precio", "cantidad", "varietal"]`).
    *   `upload_token`: ID to send back.

*   **UI - If `status == 'preview_ready'`:**
    *   Show a table of `preview_items`.
    *   Show "Confidence Score" (from `stats`).
    *   **Button:** "Confirm Import".

*   **UI - If `status == 'needs_mapping'` (or user clicks "Edit Mapping"):**
    *   Show a mapping interface.
    *   **Left Column:** Backend Fields (e.g., "Price").
    *   **Right Column:** Dropdown of `detected_columns` (e.g., "Precio Venta Publico").
    *   *Auto-select* best matches if possible.

### Step 3: Confirmation
*   **Action:** POST to `/api/catalog/confirm`.
    *   Payload:
        ```json
        {
          "upload_token": "123...",
          "mapping_override": {
            "precio": "Precio Venta",
            "sku": "Codigo"
          }
        }
        ```
*   **UI:** Success message. "Catalog is processing in background."

---

## 2. Catalog Management View (Dashboard)

**Goal:** Admin view to manage products.

*   **Filters:**
    *   Standard: Category, Price Range, Stock Status.
    *   **Dynamic/Industry:** If the user is a Winery (`rubro_slug="bodega"`), show a "Varietal" filter (derived from `extra_metadata.varietal` or `categoria`).
*   **Columns:**
    *   Image (with fallback handling).
    *   Name & Description.
    *   Price (Editable inline if possible).
    *   Stock.
    *   **External Link:** If `external_url` is present (e.g., MercadoLibre link), show it.

---

## 3. Public Marketplace (PWA)

*   **Product Cards:**
    *   Display `promocion_info` (e.g., "20% OFF") as a badge.
    *   Display `modalidad` (Sale, Donation, Exchange).
    *   **Images:** Use the `imagen_url`. If missing, use the specific category fallbacks provided by the backend API.
*   **Search:**
    *   The search bar now hits `/catalogo/buscar` which uses Qdrant. It supports semantic search (e.g., "vinos tintos baratos" works even if no product is named exactly that).

## 4. Technical Details

### Backend Endpoints

*   `POST /api/catalog/upload`: Analyzes file. Returns preview.
*   `POST /api/catalog/confirm`: Commits data.
*   `GET /catalogo`: Lists processed items.
*   `GET /catalogo/archivos`: Lists raw PDF/Excel files.

### Data Types

*   **Price:** Backend handles "1.200,00", "$1200", etc. Display as `currency` (ARS/USD).
*   **Stock:** Can be numeric or string ("Consultar").

### Error Handling

*   If Upload returns `400`: Show specific error message.
*   If Confirm returns `partial_success`: Show warnings (e.g., "5 items skipped due to missing price").
