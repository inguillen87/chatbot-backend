# Lógica de Chat Diferenciada

## Resumen

El sistema de chat ahora diferencia entre usuarios autenticados y anónimos para proporcionar una experiencia de usuario más fluida y personalizada.

## Flujo de Conversación

### Usuarios Autenticados

- **Identificación:** Los usuarios que han iniciado sesión son identificados automáticamente.
- **Sin Solicitud de Datos:** El chatbot no les pedirá información personal (nombre, email, etc.) o de ubicación, ya que estos datos se obtienen de su perfil.
- **Acceso Completo:** Tienen acceso a todas las funcionalidades del chatbot sin las limitaciones impuestas a los usuarios anónimos.

### Usuarios Anónimos

- **Identificación:** Los usuarios que interactúan con el chatbot sin iniciar sesión son tratados como anónimos.
- **Solicitud de Datos:** Cuando la conversación lo requiera (por ejemplo, para generar un reclamo o un trámite), el chatbot solicitará la información necesaria, como nombre, email o ubicación.
- **Creación de Usuario "Invitado":**
  - **Web:** Si el usuario proporciona sus datos a través del chat web, se creará un usuario de tipo "invitado" en el sistema.
  - **WhatsApp:** Si la interacción es por WhatsApp, se creará un `ChatUser` asociado al número de teléfono.
- **Limitaciones:** Los usuarios anónimos pueden tener un número limitado de interacciones antes de que se les pida que se registren o inicien sesión.

## Implementación Técnica

- **`routes/chat.py`:** Maneja la lógica principal para diferenciar entre usuarios autenticados y anónimos.
- **`services/chat_orchestrator.py`:** Orquesta el flujo de la conversación, decidiendo si solicitar información basándose en el estado de autenticación del usuario.
- **`services/user_service.py`:** Contiene las funciones para crear usuarios "invitados" y `ChatUser`.
- **`static/js/location.js`:** Gestiona la solicitud de geolocalización en el navegador del cliente.
- **`tests/test_chat_logic.py`:** Pruebas unitarias que verifican el comportamiento correcto de la lógica de chat diferenciada.
