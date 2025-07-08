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
    flask db migrate -m "Initial migration." # Or a descriptive message for new migrations
    flask db upgrade
    ```

5.  **Run the development server:**
    ```bash
    flask run
    ```

### WhatsApp Business API Integration (`routes/whatsapp_webhook.py`)

This integration allows the chatbot to communicate via WhatsApp using the Twilio API.

**Environment Variables for WhatsApp Integration:**
Ensure the following environment variables are set in your `.env` file or server configuration (e.g., on Render, Heroku):

*   `TWILIO_ACCOUNT_SID`: Your Twilio Account SID.
*   `TWILIO_AUTH_TOKEN`: Your Twilio Auth Token.
*   `TWILIO_NUMEROS_JSON`: Path to a JSON file mapping Twilio numbers to client information.
    *   Default path if not set: `data/numeros_whatsapp.json`
    *   Example content for `numeros_whatsapp.json`:
        ```json
        {
          "+17432643718": {"empresa_id": 1, "nombre": "Empresa Ejemplo 1", "tipo": "municipio"},
          "+14153278900": {"empresa_id": 2, "nombre": "Negocio Ejemplo 2", "tipo": "pyme"}
        }
        ```
        Ensure this JSON file is present at the specified path.

**Webhook Configuration:**
*   The webhook endpoint is exposed at `/webhook/whatsapp`.
*   In your Twilio console, for each provisioned WhatsApp number, set the "A MESSAGE COMES IN" webhook URL to `https://<your_domain>/webhook/whatsapp` (ensure HTTPS).

**Testing the WhatsApp Integration:**
1.  Ensure your Flask application is running and accessible via HTTPS.
2.  Verify the `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_NUMEROS_JSON` environment variables are correctly set.
3.  Confirm the `numeros_whatsapp.json` file exists, is correctly formatted, and contains the Twilio number you are testing with.
4.  Send a message from your WhatsApp account to the provisioned Twilio number.
5.  Check the Flask application logs for incoming message details and any errors.
6.  The application should (currently, with stubbed logic) echo back your message.
7.  Check Twilio's console logs for details on the webhook request and response if issues arise.

**Important Notes for WhatsApp Integration:**
*   The `RequestValidator` in `routes/whatsapp_webhook.py` uses `TWILIO_AUTH_TOKEN` to validate incoming webhook signatures. This is crucial for security.
*   The current implementation uses stubbed functions for session management (`recuperar_sesion`, `guardar_sesion`) and chatbot logic (`responder_chatboc`). These will need to be replaced with actual implementations for full functionality.
*   The `twilio` library is used for sending messages. Ensure it's listed in `requirements.txt`.

### Token de Empresa para Widget Embebido (`X-Entity-Token`)

**Contexto:** El widget de chat embebido utiliza un token para identificar a la empresa (PYME/Municipio).

**Implementación Actual:**
*   El token es el `User.token` del usuario administrador de la entidad.
*   Se envía en la cabecera `X-Entity-Token`.

**Advertencia de Seguridad:**
*   El `User.token` del administrador otorga privilegios de administrador.
*   **NO SE RECOMIENDA INCRUSTAR ESTE TOKEN DIRECTAMENTE EN CÓDIGO HTML/JS PÚBLICO.**
*   Las empresas deben cargar el token desde una variable de entorno en su propio servidor y pasarlo de forma segura al widget o actuar como proxy.

**Recomendación Futura:**
*   Implementar tokens de API específicos para widgets con permisos restringidos.

**Agentes IA:**
*   Al trabajar en el widget embebido o autenticación de entidades, considerar la sensibilidad del token.

### Running Tests
Unit tests are located in the `tests/` directory.
To run all tests:
```bash
python -m unittest discover tests
```
To run a specific test file:
```bash
python -m unittest tests/test_your_module.py
```
Ensure all dependencies, including development/test dependencies, are installed before running tests. Due to sandbox limitations, `pip install` might time out; run it locally if needed.

### Code Style and Linting
(Placeholder for future instructions on linters or formatters if adopted, e.g., Black, Flake8)

Remember to consult `README.md` for general project information.
