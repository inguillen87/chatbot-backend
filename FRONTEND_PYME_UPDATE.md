# Frontend Pyme Update Package

This document outlines the API changes, data structure updates, and logic requirements for the **Pyme Smart Agent** update. This upgrade aims to provide a "World Class SaaS" experience with multimodal capabilities and intelligent sales features.

## 1. Smart Features (UX/UI)

### 1.1 Visual Search & Document Analysis
The bot now accepts images of products, handwritten notes, and invoices.
*   **UI Requirement:** When a user uploads an image, the chat interface **must** show a processing state beyond just "Sent".
    *   **State 1 (Upload):** Show thumbnail with spinner.
    *   **State 2 (Processing):** Display a small toast or inline indicator: *"Analizando imagen..."* or *"Leyendo tu pedido..."*.
    *   **State 3 (Response):** The bot will reply with extracted items (e.g., *"Leí tu nota: 2 Malbec y 1 Queso. ¿Confirmo?"*).

### 1.2 Proactive Suggestions (Toast/Bubble)
The backend now generates intelligent cross-selling suggestions (`sugerencia_proactiva`) appended to messages.
*   **UI Recommendation:** Instead of just appending text, parse lines starting with `💡 Tip:` or similar markers and display them as a **dismissible toast** or a distinct **"Smart Hint" bubble** above the input bar.
    *   *Example:* "💡 Tip: Un buen queso haría el maridaje perfecto." -> [Ver Quesos] button.

### 1.3 Location Widget
If the user asks "Dónde están?", the bot returns `accion_backend: "pyme_ubicacion"` (or similar via text).
*   **UI Requirement:** If the response includes location data (`lat`, `long`), render a **Mini Map Widget** (using Google Maps Static API or similar) directly in the chat stream, with a button *"Como llegar"* that opens the native maps app.

## 2. New & Updated API Endpoints

### 2.1 Ticket Categories (Admin)
*   **Endpoint:** `GET /admin/tenants/<slug>/ticket-categories`
*   **Use:** Populate dropdowns in Admin Panel.

### 2.2 Municipal Posts (Public)
*   **Endpoint:** `GET /municipal/posts`
*   **Use:** Public news feed.

### 2.3 Catalog Import (Admin)
*   **Endpoint:** `POST /api/admin/catalogo/importar`
*   **Use:** Bulk update prices/stock via CSV/Excel.

## 3. Data Logic & "Mirror Catalog"

### 3.1 External Links ("Mirror Catalog")
Items can now point to external marketplaces (MercadoLibre, TiendaNube).
*   **Check:** `item.checkout_type` (`mercadolibre` | `tiendanube` | `chatboc`).
*   **Render:**
    ```jsx
    if (item.checkout_type === 'mercadolibre') {
      return <Button icon="ml" onClick={() => window.open(item.external_url)}>Comprar en ML</Button>;
    }
    // Default internal cart
    return <Button onClick={addToCart}>Agregar</Button>;
    ```

### 3.2 Chat Buttons (`type: "url"`)
The Chat API (`/chat`) may return buttons with `type: "url"`.
*   **Action:** These must open a new tab, NOT send a postback to the bot.

## 4. Tenant Isolation Details

### 4.1 Ticket IDs
*   **Pyme:** Integers (e.g., `#1024`).
*   **Municipio:** UUIDs (e.g., `a1b2...`).
*   **UI:** Adapt the ID column width/format accordingly.

### 4.2 "Punto Limpio" Banner
*   **Strict Rule:** Only render the Recycling Banner if `tenant.tipo === 'municipio'`. It is now strictly filtered by the backend, but the frontend must respect the tenant type to avoid layout shifts.

## 5. Testing the "Smart Agent"
1.  **Handwriting:** Upload a photo of a handwritten list ("1 pan, 2 leches"). The bot should parse it and offer to add to cart.
2.  **Product Photo:** Upload a photo of a wine bottle. The bot should identify it and show price/stock.
3.  **Cross-Sell:** Add a "Vino" to cart. The bot should suggest "Quesos" or "Copas" in the next response.
