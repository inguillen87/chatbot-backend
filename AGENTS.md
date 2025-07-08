## Working with this Chatboc API Project

This document provides guidance for AI agents working on this codebase.

### General Conventions
- Follow standard Python coding conventions (PEP 8).
- Write clear and concise commit messages.
- Ensure new features are accompanied by relevant tests.
- Update this `AGENTS.MD` if you introduce changes that require new setup or specific operational knowledge.

### Running the Flask Application
1.  **Set up a Python virtual environment:**
    ```bash
    python -m venv venv
    source venv/bin/activate  # On Windows: venv\Scripts\activate
    ```
2.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```
3.  **Set up Environment Variables:**
    Create a `.env` file in the project root. See `config.py` for all possible variables. Essential ones include:
    *   `FLASK_APP=app.py`
    *   `FLASK_ENV=development` (or `production`)
    *   `SECRET_KEY=your_super_secret_key`
    *   `SQLALCHEMY_DATABASE_URI=your_database_url` (e.g., `postgresql://user:pass@host/dbname` or `sqlite:///instance/database.db`)
    *   `LOG_LEVEL=INFO` (or `DEBUG`)

4.  **Database Migrations:**
    If this is the first time or there are new migrations:
    ```bash
    flask db init  # If first time initializing migrations
    flask db migrate -m "Your descriptive migration message."
    flask db upgrade
    ```
    After adding new models (like `WhatsappNumero`), ensure you run `flask db migrate` and `flask db upgrade`.

5.  **Run the development server:**
    ```bash
    flask run
    ```

### WhatsApp Business API Integration (`routes/whatsapp_webhook.py`)

This integration allows the chatbot to communicate via WhatsApp using the Twilio API. The mapping of Twilio numbers to client accounts (User model where rol is 'empresa' or 'municipio') is managed in the database via the `WhatsappNumero` table.

**Database Model: `WhatsappNumero`**
*   **Purpose:** Links an incoming Twilio WhatsApp number to a specific `User` record in the database. This `User` record represents the company or municipality associated with that WhatsApp number.
*   **Key Fields in `WhatsappNumero` model:**
    *   `numero_whatsapp`: The E.164 formatted WhatsApp number (e.g., "+14155238886").
    *   `user_id`: Foreign Key to `user.id`. This is the ID of the 'empresa' or 'municipio' type User.
    *   `is_active`: Boolean flag to enable/disable the mapping.
*   **Client Information:** The webhook retrieves client details (like name, type, and the crucial `empresa_id` which is `User.id`) by joining `WhatsappNumero` with the `User` table.
    *   Client Name: Derived from `User.nombre_empresa` or `User.name`.
    *   Client Type: Derived from `User.tipo_chat` (e.g., 'pyme', 'municipio').

**Environment Variables for WhatsApp Integration:**
Ensure the following environment variables are set in your `.env` file or server configuration:
*   `TWILIO_ACCOUNT_SID`: Your Twilio Account SID.
*   `TWILIO_AUTH_TOKEN`: Your Twilio Auth Token.

**Webhook Configuration:**
*   The webhook endpoint is exposed at `/webhook/whatsapp`.
*   In your Twilio console, for each provisioned WhatsApp number, set the "A MESSAGE COMES IN" webhook URL to `https://<your_domain>/webhook/whatsapp` (ensure HTTPS).

**Managing WhatsApp Number Mappings:**
*   **Crucial:** For the webhook to identify a client, a record must exist in the `whatsapp_numero` table linking the Twilio number (e.g., "+14155238886") to the corresponding `User.id` of the company/municipality.
*   This table needs to be populated with these mappings. This can be done via:
    *   A dedicated admin interface (to be developed).
    *   CLI commands (to be developed).
    *   Integration into the company/municipality onboarding process.
*   If a message arrives for a number not in `whatsapp_numero` or if its entry is inactive, the webhook will return a 404 error.

**Testing the WhatsApp Integration:**
1.  Ensure your Flask application is running and accessible via HTTPS.
2.  Verify the `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` environment variables are correctly set.
3.  Confirm that an active entry exists in the `whatsapp_numero` table for the Twilio number you are testing, linking it to a valid `User` (company/municipality).
4.  Send a message from your WhatsApp account to the provisioned Twilio number.
5.  Check the Flask application logs for incoming message details, client identification, and any errors.
6.  The application should (currently, with stubbed bot logic) echo back your message, including details of the identified client.
7.  Check Twilio's console logs for details on the webhook request and response if issues arise.

**Important Notes for WhatsApp Integration:**
*   The `RequestValidator` in `routes/whatsapp_webhook.py` uses `TWILIO_AUTH_TOKEN` to validate incoming webhook signatures.
*   The webhook now integrates with the application's core session management (using the `ChatSessionContext` model) and chatbot logic (by calling `services.logic.responder_chatboc`). Stubbed functions for these have been replaced. Ensure `responder_chatboc` correctly handles context and returns responses suitable for WhatsApp.

**Manual Testing Guidance for WhatsApp Integration:**
After deploying the changes:
1.  **Ensure Pre-requisites:**
    *   Verify `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` are correctly set in your environment.
    *   Confirm an active entry exists in the `whatsapp_numero` table linking your test Twilio WhatsApp number to the correct `User.id` of the target company/municipality.
    *   The Flask application must be running and accessible via HTTPS (e.g., through Render, ngrok for local testing).
    *   The Twilio console for your WhatsApp number must be configured to point its "A MESSAGE COMES IN" webhook to your application's `/webhook/whatsapp` endpoint.
2.  **Send a Test Message:**
    *   From a personal WhatsApp account, send a message to your configured Twilio WhatsApp number.
3.  **Observe Behavior & Logs:**
    *   **Application Logs:** Check your Flask application's console output (or Render logs). You should see:
        *   "Received WhatsApp message..." log with your number and message.
        *   "Mensaje para cliente..." log showing the correct company/municipality identified.
        *   Logs related to session creation/retrieval from `ChatSessionContext`.
        *   Logs indicating `responder_chatboc` is being called.
        *   The raw response from `responder_chatboc`.
        *   "Session saved for..." log.
        *   "Mensaje de respuesta enviado a..." log with a Twilio SID.
    *   **WhatsApp Response:** You should receive a response on your personal WhatsApp from the bot, generated by `responder_chatboc`.
    *   **Twilio Console Logs:** If messages are not sent/received as expected, or if Flask logs show errors sending to Twilio, check the Twilio dashboard (Messaging > Logs) for your number. It will show details of webhook requests, responses from your app, and any errors Twilio encountered delivering the message.
4.  **Test Session Persistence:**
    *   Send a follow-up message.
    *   Verify in the application logs that the existing session (`ChatSessionContext`) is found and its context is loaded.
    *   Confirm that the bot's response takes into account the previous interaction (if your bot logic supports conversational context).
5.  **Troubleshooting:**
    *   **No response / Flask errors:** Check Flask logs for tracebacks. Common issues could be:
        *   Incorrect parameters passed to `responder_chatboc`.
        *   Errors within `responder_chatboc` itself.
        *   Database errors when reading/writing `ChatSessionContext`.
        *   The `ChatSessionContext.context_data` not being structured as `responder_chatboc` expects.
    *   **Twilio Errors (e.g., 502 Bad Gateway on Twilio logs):** Often means your webhook endpoint is erroring out before sending a 200 OK, or it's timing out.
    *   **Incorrect Bot Behavior:** This would likely be an issue within `responder_chatboc` or how it interprets the session/context data.

### Token de Empresa para Widget Embebido (`X-Entity-Token`)
(This section remains relevant as is)
**Contexto:** El widget de chat embebido utiliza un token para identificar a la empresa (PYME/Municipio).
... (rest of section unchanged) ...

### Running Tests
Unit tests are located in the `tests/` directory.
To run all tests:
```bash
python -m unittest discover tests
```
To run a specific test file (e.g., for the WhatsApp webhook):
```bash
python -m unittest tests/test_whatsapp_webhook.py
```
Ensure all dependencies are installed. Note: An unrelated issue in `services/llm_utils.py` concerning `cohere.errors` may currently prevent tests from running. This needs to be addressed separately.

### Code Style and Linting
(Placeholder for future instructions)

Remember to consult `README.md` for general project information.
