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
    B --> C(services/input_processor.py);
    C --> D(services/Orchestrator);
    D --LLM Interaction (Prompt + History)--> E(services/gemini_bridge.py - Gemini LLM);
    E --Structured JSON (respuesta_usuario, accion_backend, datos_estructura, pedir_info, botones)--> D;
    D --Execute Action (datos_accion = datos_estructura)--> F(services/actions/ActionHandlers e.g., CrearReclamo, ProcesarPedido);
    F --Uses--> G[Business Logic Services (TicketService, PedidoService, CartService)];
    F --Uses--> H[External API Services (Vision, DocAI, Qdrant Clients)];
    G --DB Interaction--> I[Database (models.py)];
    H --External Calls--> J[External APIs (Google Cloud AI/Vision/DocAI, Twilio, Qdrant)];
    D --Format Response (respuesta_usuario, botones)--> K(services/response_formatter.py);
    K --Channel-Specific Response--> L[Output to User (Web/WhatsApp)];
    M[ChatSessionContext (DB - Stores llm_conversation_history, pyme_ctx, municipio_ctx)] -.-> C;
    M -.-> D;
    D -.-> M;
    F -.-> M;
    subgraph Specialized Analysis Services
        H_Vision[services/interpretacion_imagen_service.py (Vision API)]
        H_DocAI[services/google_docai.py (Document AI)]
        H_Qdrant[services/qdrant_search.py (Qdrant Client)]
        H_STT[services/external_apis/speech_to_text_service.py (STT)]
    end
    C --May Use for Voice--> H_STT;
    F --May Use for Attachments/Search--> H_Vision;
    F --May Use for Attachments/Search--> H_DocAI;
    F --May Use for Attachments/Search--> H_Qdrant;
```

1.  **User Input & Channel Handling (`routes/` & `services/input_processor.py`):**
    *   User messages arrive via channel-specific endpoints (e.g., HTTP for web, webhooks for WhatsApp).
    *   `services/input_processor.py`:
        *   Standardizes input from all channels.
        *   Extracts text, media (images, voice notes, documents), location, and interactive payloads.
        *   For voice notes, it may utilize a speech-to-text service (e.g., `services/external_apis/speech_to_text_service.py`).
        *   Loads and helps manage the `ChatSessionContext` (from `models.py`), which stores conversation history (`llm_conversation_history`) and session-specific state (`contexto_municipio`, `contexto_pyme`).

2.  **Orchestration (`services/orchestrator.py` or logic within `services/logic.py` -> `responder_municipio`/`responder_pyme`):**
    *   This is the central component coordinating the interaction.
    *   It receives standardized input.
    *   It constructs the prompt for the LLM using the current user message, `llm_conversation_history`, and other relevant context (like current pyme/municipio state).
    *   It calls `services/gemini_bridge.llamar_gemini` to communicate with the LLM.
    *   The LLM returns a structured JSON response (see `AGENTS.md` for details) containing `respuesta_usuario`, `accion_backend`, `datos_estructura`, `pedir_info`, and `botones`.
    *   The Orchestrator then decides the next step:
        *   If an `accion_backend` is specified, it invokes the corresponding **Action Handler** (from `services/actions/` or directly as an `accion_` function), passing `datos_estructura` (as `datos_accion`) and the overall `context`.
        *   If `pedir_info` is set, it prepares to ask the user for the specified information using `respuesta_usuario` and `botones` from the LLM.
        *   Handles corrections if `accion_backend` is "corregir_datos" by updating context and re-triggering confirmation/flow.
        *   Otherwise, it prepares the LLM's conversational reply.

3.  **LLM Interaction (`services/gemini_bridge.py`):**
    *   Contains the master `JULES_SYSTEM_PROMPT` which defines the LLM's persona, capabilities, expected input/output format, and examples for various scenarios including claims, orders, tool usage, and corrections.
    *   The `llamar_gemini` function handles communication with the Gemini API.
    *   `llamar_gemini_para_generacion_texto` provides a utility for more general text generation tasks with custom system prompts.

4.  **Action Handlers (`services/actions/` or `accion_` functions):**
    *   Modular functions/classes responsible for specific `accion_backend`s.
    *   Examples: `accion_crear_reclamo_municipio`, `CrearPedidoAction` (conceptual for PYMEs).
    *   Action Handlers/functions:
        *   Receive `datos_accion` (from LLM's `datos_estructura`) and the main `context`.
        *   Perform **rigorous data validation** on inputs from `datos_accion`.
        *   Interact with business logic services (e.g., `TicketService`, `PedidoService`, `CartService`).
        *   Call external API services (Qdrant, Vision, DocAI) as needed, often through intermediary services like `interpretacion_imagen_service.py`.
        *   Update the database via SQLAlchemy models.
        *   Return a result to the Orchestrator.

5.  **Business Logic Services (e.g., `services/ticket_service.py`, `services/pedido_service.py`, `services/cart.py`):**
    *   Encapsulate core operations (creating tickets, managing orders/carts). Called by Action Handlers.

6.  **Specialized Analysis & External API Services:**
    *   `services/interpretacion_imagen_service.py`: Uses `services/google_vision_service.py` to analyze images (OCR, object detection). For PYME orders, its `_procesar_interpretacion_pedido_pyme` now also uses `extraer_lista_pedido_de_texto_con_llm` from `llm_utils.py` to parse OCR text more intelligently.
    *   `services/google_docai.py` & `services/llm_utils.py` (for `analyze_document_with_google_document_ai`): For parsing structured data from documents using Google Document AI.
    *   `services/qdrant_search.py` & `services/qdrant_utils.py`: Interface with Qdrant for semantic search (e.g., PYME product catalogs, FAQs). Embeddings are generated using Cohere (via `services.cohere_ai.embed_textos`).
    *   `services/whatsapp_webhook.py` (and Twilio client): Handles sending messages via WhatsApp.
    *   `services/external_apis/speech_to_text_service.py`: For transcribing voice notes.
    *   `services/llm_utils.py`: Contains helper functions for more specific LLM tasks like `extract_multiple_contact_details_llm` or `extraer_lista_pedido_de_texto_con_llm`.

7.  **Document & Image Processing Flow (General for Attachments):**
    *   Files are uploaded, `ArchivoAdjunto` record is created.
    *   A Celery task (`services.analisis_archivo_service.tarea_analizar_contenido_archivo`) is typically triggered.
    *   This task calls `interpretar_imagen_para_chat` (for images) or `analizar_pdf_con_document_ai_service` (for PDFs).
    *   These services perform the analysis (Vision, DocAI, internal LLM calls for refinement) and update the `AnalisisArchivo` record with extracted text and structured data.
    *   The Orchestrator or relevant handlers can then use this processed information from `AnalisisArchivo` or directly from `datos_accion` if the analysis was synchronous with the LLM's main turn.

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
