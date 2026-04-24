# Frontend Tasks for Catalog Import & Widget

These tasks align with the new Backend API contracts for Import Jobs and Widget Configuration.

## 1. Catalog Import Wizard (New Flow)

### API Contract (Backend Ready)
- **POST /api/admin/catalog/import**: Uploads file. Returns `{ "upload_id": 123, "status": "processing" }`.
- **GET /api/admin/catalog/import/{id}**: Polling. Returns:
  ```json
  {
    "status": "ready_to_commit" | "failed",
    "preview_data": { "items": [...], "columns": [...], "warnings": [...] },
    "errors": ["..."],
    "stats": { "confidence": 0.85 },
    "engine_used": "llm_vision"
  }
  ```
- **PUT /api/admin/catalog/import/{id}**: Send corrected preview data (e.g. after user edits cell/column).
- **POST /api/admin/catalog/import/{id}/commit**: Finalize import.

### Tasks
1.  **Wizard Step 1: Upload**
    -   File input (accept .xlsx, .csv, .pdf, images).
    -   Show progress bar (fake it if needed, or just "Procesando...").
    -   Handle 4xx/5xx errors gracefully.

2.  **Wizard Step 2: Preview & Validation**
    -   **Critical**: If `status == "failed"`, DO NOT show empty table. Show the error message from `errors` array (e.g. "No structured data found").
    -   **Table UI**: Render `preview_data.items`.
    -   **Columns**: Use `preview_data.columns` if available, or infer keys from items.
    -   **Warnings**: Display `upload.warnings` (e.g. "Fallback to AI Vision triggered") in a yellow alert box.
    -   **Confidence**: Show "Confidence Score" badge (e.g. "Alta 90%", "Media 50%", "Baja - Revisar").

3.  **Wizard Step 3: Editing (Optional but Recommended)**
    -   Allow inline editing of cells in the table.
    -   On change, update local state.
    -   (Advanced) "Save Changes" button calls `PUT` endpoint.

4.  **Wizard Step 4: Commit**
    -   "Confirm Import" button calls `POST .../commit`.
    -   Show success message: "Imported X items successfully".

## 2. Widget Configuration (Backend Ready)

### API Contract
- **GET /api/public/tenants/{slug}/widget-config**: Returns public config (colors, welcome message).
- **GET /api/widget/config**: Authenticated config fetch.

### Tasks
1.  **Widget Preview in Admin**
    -   In "Configuración > Widget", render a live preview of the chat bubble.
    -   Reflect changes instantly (Color Picker -> update bubble color).

2.  **Embed Script**
    -   Ensure the generated script tag points to the correct endpoint/CDN.
    -   Handle `window.chatbocSettings` correctly.

## 3. General UX Improvements
-   **Empty States**: Never show just "0 items". Always explain *why* or offer "Retry".
-   **Loading States**: Skeletons instead of spinners for tables.
-   **Error Boundaries**: If the Preview table crashes due to malformed JSON, catch it and show "Data Error" instead of white screen.
