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
    *   Inside `responder_municipio`/`responder_pyme`, for relevant intents (e.g., "iniciar_reclamo", "crear_pedido") or when continuing an LLM-driven dialogue, a call is made to `services.gemini_bridge.llamar_gemini`.
    *   **`services.gemini_bridge.JULES_SYSTEM_PROMPT`**: This is the master prompt that defines the LLM's persona, capabilities, input/output structure, and examples. **This is the primary place to adjust the bot's "vocabulary", understanding, and decision-making logic.**
    *   The LLM is expected to return a JSON object with the following structure:
        ```json
        {
          "respuesta_usuario": "...respuesta profesional y directa para mostrar al usuario...",
          "accion_backend": "...crear_reclamo | consulta_estado | info_tramite | agregar_item_carrito | finalizar_pedido_pyme | derivar_humano...",
          "datos_estructura": {
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
    *   These `accion_` functions in `services/municipios.py` or `services/pymes.py` (or dedicated `actions_municipio.py`, `actions_pyme.py` modules) contain the business logic:
        *   Validate data received in `datos_llm.datos_estructura`.
        *   Interact with the database (create/update tickets, pedidos, etc.).
        *   Call external services (notifications, geocoding APIs).
        *   Return a response object for the user.
6.  **Dialog Management**:
    *   If `respuesta_llm.pedir_info` is set, the `respuesta_llm.respuesta_usuario` and `respuesta_llm.botones` are used to ask the user for more information. The conversation history (including the LLM's request for info) is passed back to the LLM in the next turn.
7.  **Fallback**: If the LLM flow is not triggered (e.g., `USAR_LLM_PARA_RECLAMOS=False`) or if the LLM fails to provide a usable response, the system may fall back to the traditional handler-based logic (which is being progressively refactored).

### Developing New Features / Modifying Behavior
1.  **Primary Tool: `JULES_SYSTEM_PROMPT` (`services/gemini_bridge.py`)**
    *   To change how the bot understands user requests, extracts information, or decides on next steps, **start by modifying this prompt.**
    *   Add more examples, clarify rules, or refine descriptions of `accion_backend` and `datos_estructura`.
    *   Ensure the prompt clearly instructs the LLM to *always* return the specified JSON structure.
2.  **Adding New Backend Actions**:
    *   Define the new `accion_backend` string (e.g., `solicitar_devolucion_producto`).
    *   Update `JULES_SYSTEM_PROMPT` to include this new action in the list of possibilities and provide examples of when/how the LLM should use it and what `datos_estructura` are expected.
    *   Create the corresponding Python function: `accion_solicitar_devolucion_producto(datos_llm: dict, context: dict)` in the relevant services module.
    *   This function will perform the actual business logic (e.g., create a return ticket, update inventory, notify logistics).
    *   Update the main orchestrator (`responder_pyme` or `responder_municipio`) to call this new action function when the LLM specifies it.
3.  **Data Validation**:
    *   Critical data validation (e.g., email format, phone number validity, valid address components *after* LLM extraction) should occur in the Python `accion_` functions.
    *   **Future Enhancement**: If validation fails, the system should ideally inform the LLM (e.g., by adding a "system_feedback" turn to the history) so the LLM can re-ask the user for correct information naturally.
4.  **Tools / Function Calling (Future)**:
    *   To allow the LLM to query real-time data (e.g., "status of my ticket #123", "is product X in stock?", "what are the opening hours for Y office today?"), integrate LLM function calling capabilities.
    *   The LLM would indicate a tool/function to call with specific parameters. The backend executes this tool and returns the result to the LLM, which then formulates the user-facing response.

### Running the Flask Application & Tests
(This section can largely remain as is, but ensure `pip install google-cloud-aiplatform` is included and credential setup for Google Cloud is mentioned.)

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
        *   `tests/test_gemini_bridge.py`: Tests the `llamar_gemini` function (mocked or real). Focus on prompt formatting and parsing of the LLM's JSON response.
        *   `tests/test_acciones_municipio.py` (and similar for pyme): Test individual `accion_` functions. Mock the `datos_llm` input and dependencies (DB, external services).
        *   **Integration Tests (within `responder_municipio`/`responder_pyme`)**: Mock `gemini_bridge.llamar_gemini` to return controlled LLM JSON responses. Verify that `responder_municipio`/`pyme` correctly calls the appropriate action functions or manages dialogue based on the LLM's output.

### Current LLM-Powered Flows (Example: Municipio Reclamos)
*   The `USAR_LLM_PARA_RECLAMOS` flag in `services/municipios.py` controls the new flow.
*   If active, `responder_municipio` calls `llamar_gemini`.
*   `accion_crear_reclamo_municipio` is called if LLM provides all necessary data.
*   A separate `historial_llm_reclamo` is maintained in the `contexto_municipio_actual` for multi-turn interactions guided by the LLM.

### WhatsApp Business API Integration & Entity Token
(These sections from the original AGENTS.md remain relevant and can be kept as is, but ensure they are placed after the new core architecture description.)

---
This document should be updated as the LLM integration evolves and more functionalities are migrated to this new pattern.
