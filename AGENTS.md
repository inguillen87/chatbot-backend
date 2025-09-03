## Working with this Chatboc API Project (LLM-Powered Architecture)

This document provides guidance for AI agents and human developers working on this codebase, which is transitioning to an architecture primarily driven by a Large Language Model (LLM) like Google's Gemini.

### Core Architectural Philosophy
The primary goal is to centralize language understanding, conversation flow management, and data extraction logic within the LLM, guided by a comprehensive system prompt. The Python backend should act as an executor of actions determined by the LLM and a manager of data persistence and external service interactions.

**Key Principle:** The LLM is responsible for the "intelligence" and "comprehension"; Python code is responsible for "execution" and "validation" of structured data. Avoid adding complex language parsing, keyword-based logic, or extensive if-else chains for intent recognition in Python.

### Main Chat Flow
1.  **User Input**: Messages are received حياة `routes/chat.py` (endpoints `/ask`, `/ask/pyme`, `/ask/municipio`).
2.  **Context Management**: `routes/chat.py` loads/manages `ChatSessionContext` to maintain conversation history and state across turns.
3.  **Orchestration**: The request is passed to `services.logic.responder_chatboc`, which then routes to:
    *   `services.municipios.responder_municipio` for municipal interactions.
    *   `services.pymes.responder_pyme` for business interactions.
4.  **LLM Interaction (New Flow)**:
    *   Inside `responder_municipio`/`responder_pyme`, for relevant intents (e.g., "iniciar_reclamo", "crear_pedido") or when continuing an LLM-driven dialogue, a call is made to `services.llm_bridge.llamar_llm`.
    *   **`services.llm_bridge.JULES_SYSTEM_PROMPT`**: This is the master prompt that defines the LLM's persona, capabilities, input/output structure, and examples. **This is the primary place to adjust the bot's "vocabulary", understanding, and decision-making logic.**
    *   The LLM is expected to return a JSON object with the following structure:
        ```json
        {
          "respuesta_usuario": "...respuesta profesional y directa para mostrar al usuario...",
          "accion_backend": "...crear_reclamo | consulta_estado_ticket | info_tramite | agregar_item_carrito | finalizar_pedido_pyme | ejecutar_herramienta | corregir_datos | derivar_humano...",
          "datos_estructura": { // This becomes 'datos_accion' in the Python context for handlers
            "target": "municipio | pyme | ambos", // Crucial for routing/context
            "categoria": "...",
            "descripcion": "...",
            "ubicacion": "...",
            "coordenadas": {"lat": "...", "lon": "..."},
            "usuario": "Nombre Usuario",
            "telefono": "...",
            "email": "...",
            "target": "municipio | pyme",
            // ... otros campos específicos de la acción ...
          },
          "pedir_info": null | "ubicacion" | "categoria" | "id_reclamo" | "producto" | "email_cliente" | ...,
          "botones": [ { "texto": "Botón 1" }, { "texto": "Botón 2", "action_id": "accion_especifica" } ]
        }
        ```
5.  **Action Execution**:
    *   If `respuesta_llm.accion_backend` is set, `responder_municipio`/`responder_pyme` calls a corresponding Python function (e.g., `accion_crear_reclamo_municipio(datos_llm, context)`).
    *   Note: `datos_accion` passed to these Python functions is typically the content of `respuesta_llm.datos_estructura`.
    *   These `accion_` functions (or more structured Action Handlers in `services/actions/`) contain the business logic:
        *   Validate data received in `datos_accion`. **Python handlers are the ultimate authority on data validity and security before acting on it.**
        *   Interact with the database (create/update tickets, pedidos, etc.).
        *   Call external services (notifications, geocoding APIs).
        *   Return a response object for the user.
6.  **Dialog Management & Corrections**:
    *   If `respuesta_llm.pedir_info` is set, the `respuesta_llm.respuesta_usuario` and `respuesta_llm.botones` are used to ask the user for more information.
    *   If the LLM interprets a user message as a correction (e.g., "No, the address is X"), it should use an `accion_backend` like `corregir_datos` and provide `campo_a_corregir` and `nuevo_valor` in `datos_estructura`. The relevant Python handler then updates the stored information and typically re-confirms.
    *   The conversation history (including LLM's requests and user clarifications) is passed back to the LLM in subsequent turns to maintain context.
7.  **Fallback & Handler-Based Logic**:
    *   If the LLM flow is not triggered (e.g., for very simple, hardcoded commands or specific UI actions) or if the LLM's response is too generic (e.g., `accion_backend: "no_accion"`), the system may fall back to keyword-based logic within specific handlers (e.g., `IntentClassifierHandler` in `services/municipios.py`).
    *   This handler-based logic is being progressively refactored to support, rather than duplicate, the LLM's primary role.

### Developing New Features / Modifying Behavior
1.  **Primary Tool: `JULES_SYSTEM_PROMPT` (`services/llm_bridge.py`)**
    *   To change how the bot understands user requests, extracts information, or decides on next steps, **start by modifying this prompt.**
    *   Add more examples (including for corrections and disambiguation), clarify rules, or refine descriptions of `accion_backend` and `datos_estructura`.
    *   Ensure the prompt clearly instructs the LLM to *always* return the specified JSON structure.
2.  **Adding New Backend Actions**:
    *   Define the new `accion_backend` string (e.g., `solicitar_devolucion_producto`).
    *   Update `JULES_SYSTEM_PROMPT` to include this new action, examples of when/how the LLM should use it, and what `datos_estructura` are expected.
    *   Create the corresponding Python function: `accion_solicitar_devolucion_producto(datos_accion: dict, context: dict)` in the relevant services module or as a dedicated Action Handler in `services/actions/`.
    *   This function will perform the actual business logic. **It must validate all inputs from `datos_accion` before execution.**
    *   Update the main orchestrator (`responder_pyme`, `responder_municipio`, or a central dispatcher if refactored) to call this new action function.
3.  **Data Validation**:
    *   The LLM can perform initial data extraction, but **final validation and sanitization MUST occur in the Python `accion_` functions or Action Handlers** before database interaction or calling external services. Do not trust LLM-extracted data without backend validation.
    *   **Correction Flow**: If validation fails in Python, the system should ideally inform the LLM (e.g., by setting a specific state or providing feedback in the next LLM call context) so the LLM can re-ask the user for correct information naturally, rather than the Python code generating rigid error messages.
4.  **Tools / Function Calling (LLM-driven)**:
    *   The `JULES_SYSTEM_PROMPT` already guides the LLM to use `accion_backend: "ejecutar_herramienta"` with `nombre_herramienta` and `parametros_herramienta`.
    *   The backend (`ToolHandler` in `services/municipios.py` or `services/pymes.py`) executes these.
    *   Ensure tools are well-defined, and their descriptions in the prompt are clear for the LLM.

### Running the Flask Application & Tests
(This section can largely remain as is, but ensure `pip install google-cloud-aiplatform google-cloud-vision google-cloud-documentai qdrant-client` are included and credential setup for Google Cloud is mentioned.)

1.  **Set up a Python virtual environment & Install Dependencies:**
    ```bash
    python -m venv venv
    source venv/bin/activate  # On Windows: venv\Scripts\activate
    pip install -r requirements.txt
    # Ensure google-cloud-aiplatform is in requirements.txt or install separately:
    # pip install google-cloud-aiplatform
    ```
2.  **Environment Variables:**
    Create a `.env` file. Key variables:
    *   `FLASK_APP=app.py`
    *   `FLASK_ENV=development`
    *   `SECRET_KEY=your_secret_key`
    *   `SQLALCHEMY_DATABASE_URI=your_database_url`
    *   `GOOGLE_APPLICATION_CREDENTIALS=/path/to/your/gcp-credentials.json` (if not using default ADC)
    *   (Twilio, Cohere, and other API keys as needed)
3.  **Database Migrations & Run Server**: (As before)
    ```bash
    flask db upgrade
    flask run
    ```
4.  **Running Tests**:
    *   Unit tests are in `tests/`.
    *   To run all: `python -m unittest discover tests`
    *   To run specific file: `python -m unittest tests/test_file_name.py`
    *   **LLM-related tests**:
        *   `tests/test_llm_bridge.py`: Tests the `llamar_llm` function (mocked or real). Focus on prompt formatting and parsing of the LLM's JSON response.
        *   `tests/test_acciones_municipio.py` (and similar for pyme): Test individual `accion_` functions. Mock the `datos_llm` input and dependencies (DB, external services).
        *   **Integration Tests (within `responder_municipio`/`responder_pyme`)**: Mock `llm_bridge.llamar_llm` to return controlled LLM JSON responses. Verify that `responder_municipio`/`pyme` correctly calls the appropriate action functions or manages dialogue based on the LLM's output.

### Current LLM-Powered Flows (Example: Municipio Reclamos)
*   The `USAR_LLM_PARA_RECLAMOS` flag in `services/municipios.py` controls the new flow.
*   If active, `responder_municipio` calls `llamar_llm`.
*   `accion_crear_reclamo_municipio` is called if LLM provides all necessary data.
*   A separate `historial_llm_reclamo` is maintained in the `contexto_municipio_actual` for multi-turn interactions guided by the LLM.

### WhatsApp Business API Integration & Entity Token
(These sections from the original AGENTS.md remain relevant and can be kept as is, but ensure they are placed after the new core architecture description.)

---
This document should be updated as the LLM integration evolves and more functionalities are migrated to this new pattern.
