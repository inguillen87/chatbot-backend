# Chatbot Backend

This project exposes several endpoints to process questions for different sectors.

## Endpoints

- `POST /ask` – generic handler that decides the logic according to the provided sector (`rubro`).
- `POST /ask/pyme` – optimized for small and medium companies (pymes).
- `POST /ask/municipio` – optimized for municipalities and other public entities.
- `POST /auth/widget/register` – quick sign up for end users using the widget.
- `POST /auth/widget/login` – login for end users without leaving the widget.
- `POST /auth/google-login` – login o registro utilizando un ID token de Google.
- `GET /auth/token-info` – returns the company and sector linked to a token.
- `PUT /auth/me` – update the logged in user's profile.
- `GET /tickets/mios` – list the tickets created by the logged in user.
- `GET /crm/clientes` – for admins, returns the users associated with their token. Supports `?tag=` filtering.
- `PUT /crm/clientes/<id>/tags` – update the segmentation tags of a client.
- `GET /crm/analytics` – basic stats of registered users and tickets.
- `POST /crm/campanas/enviar` – mock endpoint to send campaigns to selected users.

## Variables de entorno

Configura `GOOGLE_OAUTH_CLIENT_ID` con el ID de cliente de tu aplicación de
Google (o varios separados por comas) para que el backend valide la audiencia
de los tokens enviados a `/auth/google-login`.

## Uso correcto del rubro

El archivo `services/logic.py` define el conjunto `RUBROS_PUBLICOS` con los rubros que se tratan como entes públicos, por ejemplo `"municipio"` y `"municipios"`.

Si el rubro enviado pertenece a este conjunto, el tipo de chat correspondiente es `"municipio"`. Debe usarse el endpoint `/ask/municipio` y el campo `tipo_chat` igual a `"municipio"`. Enviar un rubro público al endpoint `/ask/pyme` resultará en un ajuste automático o un error según la configuración.

El backend prioriza siempre el rubro para elegir la lógica. Si el token o el
rubro indicado corresponde a un municipio, se utilizará la lógica de municipio
aun cuando el `tipo_chat` recibido sea "pyme". De igual manera, un rubro de pyme
forzará el uso de la lógica de pyme. No existe un tipo por defecto: si no se
puede determinar el rubro ni se envía un `tipo_chat` válido, la petición devuelve
un error claro.

## Registro en el widget

Las funciones premium como el chat en vivo o el guardado de la ubicación requieren que el usuario esté autenticado. El registro puede hacerse sin salir del chat enviando un `POST /auth/widget/register` con el token de la pyme o municipio en el encabezado `Authorization`. El backend asociará automáticamente al nuevo usuario con esa entidad y registrará si acepta recibir comunicaciones de marketing.

Los tickets creados desde el widget ahora se asignan al usuario final (campo `cliente_id`) en lugar de al dueño del token, permitiendo que cada ciudadano o cliente consulte luego su historial.

### Formas de enviar el token

El backend acepta el token en cualquiera de los siguientes lugares de la solicitud:

1. Encabezado `Authorization` con formato `Bearer <token>`.
2. Encabezado `X-Token`.
3. Encabezado `X-Entity-Token` (compatibilidad con versiones previas).
4. Parámetro de query string `?token=...`.
5. Campo `token` dentro del JSON o formulario enviado.

Esto permite embebidos del widget que envíen el token como atributo o en la URL sin necesidad de modificar el backend.

### Migración de tickets anónimos

Si el usuario crea tickets en el widget antes de registrarse, guarda un
identificador anónimo en el navegador (`Anon-Id`). Al enviar ese valor en el
header `Anon-Id` durante la llamada a `POST /auth/widget/register`, el backend
migrará automáticamente esos tickets y comentarios para que pertenezcan al nuevo
usuario.

## Mejoras recientes

- Los listados de productos del catálogo y los pedidos se ordenan por precio para que sea más fácil elegir.
- Cada artículo consolida su código, descripción y precio en una sola línea para evitar datos fragmentados.
- El historial de chat para pymes ahora conserva hasta 30 mensajes para dar más contexto en cada respuesta.
  Nuevo módulo de validaciones que comprueba nombre, correo y teléfono usando librerías open source.
- Si no puedes modificar `requirements.txt`, instala manualmente `email-validator` y `phonenumbers` para habilitar estas validaciones.
