# System Architecture Overview

This document outlines the high-level architecture of the Chatboc API, which is designed as an LLM-Powered system with modular components for handling interactions across different channels (web, WhatsApp) and for various entities (Municipios, PYMEs).

## Core Philosophy

The system's intelligence, natural language understanding (NLU), conversational flow management, and initial data extraction are primarily driven by a Large Language Model (LLM) - Google's Gemini. The Python backend serves as a robust executor of commands derived from the LLM's interpretations, a manager of data persistence, and an integrator with external services.

**Key Principle:**
*   **LLM (Brain):** Responsible for comprehension, dialogue management, intent recognition, and structured data extraction. Configured mainly via `services/gemini_bridge.py` and its `JULES_SYSTEM_PROMPT`.
*   **Python Backend (Executor):** Responsible for validating data, executing defined actions, interacting with databases, calling external APIs, and formatting channel-specific responses.

## Main Components and Flow

The system is structured around several key service modules:

```mermaid
graph TD
    A[User Input (Web/WhatsApp)] --> B(Routes - chat.py / whatsapp_webhook.py);
    B --> C{services/InputProcessor};
    C --> D{services/Orchestrator};
    D --LLM Interaction--> E(services/gemini_bridge.py - Gemini LLM);
    E --Structured JSON (action, data, reply)--> D;
    D --Execute Action--> F(services/actions/ActionHandlers);
    F --Uses--> G[Business Logic Services (TicketService, PedidoService, CartService, etc.)];
    F --Uses--> H[External API Services (Vision, DocAI, Qdrant, WhatsApp Sender, STT)];
    G --DB Interaction--> I[Database (models.py)];
    H --External Calls--> J[External APIs (Google Cloud, Twilio, Qdrant)];
    D --Format Response--> K(services/ResponseFormatter);
    K --Channel-Specific Response--> L[Output to User (Web/WhatsApp)];
    M[ChatSessionContext (DB)] -.-> C;
    M -.-> D;
    D -.-> M;
    F -.-> M;
```

1.  **User Input & Channel Handling (`routes/` & `services/input_processor.py`):**
    *   User messages arrive via channel-specific endpoints (e.g., HTTP for web, webhooks for WhatsApp).
    *   `services/input_processor.py`:
        *   Standardizes input from all channels.
        *   Extracts text, media (images, voice notes, documents), location, and interactive payloads.
        *   For voice notes, it utilizes `services/external_apis/speech_to_text_service.py` for transcription.
        *   Loads and helps manage the `ChatSessionContext` (from `models.py`), which stores conversation history and session-specific state.

2.  **Orchestration (`services/orchestrator.py`):**
    *   This is the central nervous system of the application.
    *   It receives standardized input from the `InputProcessor`.
    *   It constructs the prompt for the LLM using the current user message, conversation history (from `ChatSessionContext.context_data.llm_conversation_history`), and other relevant context.
    *   It calls `services/gemini_bridge.llamar_gemini` to communicate with the LLM.
    *   The LLM returns a structured JSON response containing:
        *   `respuesta_usuario`: Text to display to the user.
        *   `accion_backend`: A specific action for the backend to perform (e.g., "crear_reclamo_municipio", "procesar_adjunto_imagen_reclamo").
        *   `datos_estructura`: Data extracted by the LLM for the action.
        *   `pedir_info`: If the LLM needs more information from the user.
        *   `botones`: Suggested interactive buttons for the user.
    *   The Orchestrator then decides the next step based on the LLM's response:
        *   If an `accion_backend` is specified, it invokes the corresponding **Action Handler** from `services/actions/`.
        *   If `pedir_info` is set, it prepares to ask the user for the specified information.
        *   Otherwise, it prepares the LLM's conversational reply.

3.  **LLM Interaction (`services/gemini_bridge.py`):**
    *   Contains the master `JULES_SYSTEM_PROMPT` which defines the LLM's persona, capabilities, expected input/output format, and examples. This prompt is critical for guiding the LLM's behavior.
    *   The `llamar_gemini` function handles the actual communication with the Gemini API.

4.  **Action Handlers (`services/actions/`):**
    *   A directory of modular handlers, each responsible for a specific `accion_backend`.
    *   Examples:
        *   `municipio_claim_actions.py` (e.g., `CrearReclamoAction`): Handles creating municipal claims.
        *   `pyme_order_actions.py` (e.g., `AgregarItemCarritoAction`, `CrearPedidoAction`): Handles PYME order operations.
        *   `common_actions.py` (e.g., `DerivarHumanoAction`, `ProcesarAdjuntoAction`): Handles actions common to multiple flows.
    *   Action Handlers:
        *   Receive `datos_estructura` from the Orchestrator.
        *   Perform detailed data validation.
        *   Interact with business logic services (e.g., `TicketService`, `PedidoService`, `CartService`).
        *   Interact with `services/external_apis/` clients (e.g., `QdrantService`, `DocumentProcessorService`).
        *   Update the database via SQLAlchemy models.
        *   Return a result to the Orchestrator (success/failure, messages, data).

5.  **Business Logic Services (Various, e.g., `services/ticket_service.py`, `services/pedido_service.py`):**
    *   These services encapsulate core business operations like creating a ticket, creating an order, managing cart contents, etc. They are called by Action Handlers.

6.  **External API Services (`services/external_apis/`):**
    *   Dedicated client modules for interacting with third-party APIs:
        *   `google_vision_service.py`: For image analysis.
        *   `google_doc_ai_service.py`: For document parsing.
        *   `qdrant_service.py`: For vector database search (product catalogs, FAQs).
        *   `whatsapp_service.py`: For sending messages via the WhatsApp Business API (e.g., Twilio).
        *   `speech_to_text_service.py`: For transcribing voice notes.

7.  **Document Processing (`services/document_processor.py`):**
    *   Coordinates the use of Vision API and Doc AI.
    *   Called by `ProcesarAdjuntoAction` when an uploaded file needs analysis.
    *   Stores structured analysis results in the `AnalisisArchivo` model, which can then be used by the LLM or other actions.

8.  **Response Formatting & Delivery (`services/response_formatter.py`):**
    *   Takes the internal response object from the Orchestrator (containing text, button data, media URLs, etc.).
    *   Formats it appropriately for the target channel (web UI or WhatsApp).
    *   For WhatsApp, this involves constructing the correct payloads for text messages, interactive messages (buttons, lists), and media messages, then using `WhatsAppService` to send them.

9.  **Data Persistence (`models.py`, `ChatSessionContext`):**
    *   SQLAlchemy models define the database schema.
    *   `ChatSessionContext` is crucial for storing session-specific data across multiple turns, including:
        *   `llm_conversation_history`: The history of interactions with the LLM for the current session.
        *   `estado_conversacion_llm`, `info_esperada_llm`: State related to LLM-driven dialogues.
        *   Application-specific context for Municipio or PYME flows (e.g., partially filled claim data, cart contents).
    *   Action Handlers and Business Logic Services perform CRUD operations on other models like `MunicipioTicket`, `PymePedido`, `CatalogoItem`, `ArchivoAdjunto`, `AnalisisArchivo`.

## Key Benefits of this Architecture

*   **Modularity:** Components are well-defined and have clear responsibilities, making the system easier to understand, develop, and maintain.
*   **Scalability:** Individual services can be scaled independently.
*   **Flexibility:** Easier to add new functionalities, support new channels, or integrate new external APIs by adding new Action Handlers or API clients.
*   **Centralized Intelligence:** The LLM acts as the primary NLU and dialogue engine, allowing for more natural and adaptive conversations.
*   **Testability:** Individual modules (InputProcessor, Orchestrator, ActionHandlers, API Clients) can be unit-tested more effectively by mocking their dependencies.

This architecture aims to create a robust, intelligent, and adaptable platform for providing advanced chatbot services to both municipalities and PYMEs.
```
