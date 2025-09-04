# Chatbot Backend

This project exposes several endpoints to process questions for different sectors.

When WhatsApp interactive menus are not approved or available, the bot falls back
to a text-based menu that groups options by category and includes numeric
selection instructions to maintain full visibility of all choices.

## Endpoints

- `POST /ask` – generic handler that decides the logic according to the provided sector (`rubro`).
- `POST /ask/pyme` – optimized for small and medium companies (pymes).
- `POST /ask/municipio` – optimized for municipalities and other public entities.
- `POST /widget/register` – quick sign up for end users using the widget.
- `POST /widget/login` – login for end users without leaving the widget.
- `POST /chatuserregisterpanel` – register a chat user selecting the company token.
- `POST /chatuserloginpanel` – login a chat user specifying the company token.
- `POST /google-login` – login o registro utilizando un ID token de Google. Si
  el correo no existe se crea un usuario nuevo y puede enviarse `rol` y
  `tipo_chat` para definir sus permisos (`admin` o `usuario`) y el tipo de chat
  (`pyme` o `municipio`).  Cuando el usuario aún no tiene rubro asociado la
  respuesta será `{"status": "falta_rubro", "token": "...", "email": "..."}`
  para que el frontend redirija a la selección de rubro.
- `GET /token-info` – returns the company and sector linked to a token. Consulta `docs/verificar-token.md` si necesitas comprobar que tu token es válido.
- Si el backend registra "token inválido" o la ruta `/chatuserregisterpanel` responde 404, revisa `docs/token-invalid-troubleshooting.md` para pasos de diagnóstico.
- Returned JSON now includes `rol` and `empresa_id` so the frontend can show the
  appropriate admin or employee options. Login endpoints also return the user's
  `rubro` and the suggested `tipo_chat` ("pyme" o "municipio") so the UI can
  select the correct chat flow.
- New decorators `require_role` and `require_municipio_access` help secure endpoints that depend on the user's municipality.
- `POST /subir_catalogo` – upload a product catalog (PDF, Excel or images).
- `GET /catalogo/archivos` – list available catalog files.
- `GET /catalogo/archivo/<nombre>` – download a catalog file.
- Cart management endpoints under `/carrito` allow adding, updating and
  removing products. Use `/carrito/resumen` to retrieve the current cart.
- Universal catalog search formats results for any industry using
  `armar_respuesta_legible_multi_rubro`.
- `GET /api/whatsapp/promocionar` – check whether a broadcast can be sent and
  when the last send occurred.
- `POST /api/whatsapp/promocionar` – broadcast a flyer to marketing subscribers
  of the authenticated company. Accepts `titulo`, `descripcion`, `link` and
  `url_imagen` (or a prebuilt `mensaje`). Limited to one send per day por
  empresa. Super admins can include `{"todos": true}` to enviar a todos los
  tenants y el límite diario se aplica globalmente.
- `GET /catalogo/buscar` – query the vector catalog with `?q=` and optional
  `?limite=` to control how many products are returned (defaults to
  `CATALOGO_RESULT_LIMIT`).

- `PUT /me` – update the logged in user's profile.
- `GET /me` or `/perfil` – retrieve the full profile. The response includes the user's role
  (`rol`), associated company (`empresa_id`) and the ticket categories assigned
  to the account (`categorias`).
- `GET /tickets` – for admins or employees, list all tickets for the company or
  municipality. Supports optional `?estado=` and `?categoria=` filters.
- `GET /tickets/mios` – list the tickets created by the logged in user.
- Municipal accounts hitting `/pedidos` receive the same JSON structure as
  `/tickets` to ease frontend integration.
- `GET /crm/clientes` – for admins, returns the users associated with their token. Supports `?tag=` filtering and now `?q=` search by name or email plus `?marketing=true|false`. The same data is available at `/municipal/usuarios`.
- `PUT /crm/clientes/<id>/tags` – update the segmentation tags of a client.
- `GET /crm/clientes/<id>/interacciones` – history of chats and tickets for a client.
- `GET /crm/analytics` – basic stats of registered users and tickets.
- `POST /crm/campanas/enviar` – mock endpoint to send campaigns to selected users.
- `POST /tickets/<tipo>/<id>/encuesta` – submit satisfaction survey for a ticket.
- `GET /tickets/<tipo>/<id>/encuesta` – retrieve survey results for a ticket.
- `GET /tickets/<tipo>/mapa` – list open tickets with latitude and longitude.
- `GET /estadisticas/reclamos` – statistics of tickets by category and type.
- `GET /estadisticas/usuarios/ubicaciones` – coordinates of users in the same municipality for heatmaps.
- `GET /tramites` – list available municipal procedures, supports `?q=` filtering.
- `GET /tramites/<nombre>` – detailed info for a specific procedure.
- `GET /tramites/descargar` – download the full JSON catalog of procedures.
- `GET /catalogo/descargar` – download the latest catalog file of the authenticated company.
- `GET /catalogo/resumen` – summary of products grouped by category.
- `GET /municipal/incidents` – list open municipal incidents for the authenticated user's municipality.
- `GET /municipal/stats` – professional analytics of municipal tickets including totals, categories and response time.
- `GET /municipal/metrics` – counts of neighbor messages this week, month and year.

### Municipal incidents

Admins and municipal employees can consult all open tickets of their
municipality with `GET /municipal/incidents`. The JSON response contains
fields like `id`, `nro_ticket`, `asunto`, `categoria`, `estado`, `fecha`,
`direccion`, `latitud`, `longitud`, as well as the original `pregunta`, any
`detalles` provided and the `archivo_url` if the ticket incluye un adjunto.

### Employee management

- `GET /empleados` – list internal employees associated with the token **(admin only)**.
- `POST /empleados` – create a new internal employee **(admin only)**.
- `GET /empleados/<id>/historial` – list tickets handled by an employee **(admin only)**.
- `GET /empleados/<id>` – retrieve a single employee's info **(admin only)**.
- `PUT /empleados/<id>` – update name, email, password or categories of an employee **(admin only)**.
- `DELETE /empleados/<id>` – remove an employee **(admin only)**.
- Employees can be limited to specific ticket categories using the `categorias`
  field when creating or updating them.
- `GET /historial` – retrieve the logged user's full history of chats and tickets.
- `POST /archivos/subir` – upload files associated with chats or tickets.  Use the
  `archivos` field for multiple files or `archivo` for a single file. Only images,
  PDFs, spreadsheets and text documents up to 10MB are accepted.
- `GET /archivos/<nombre>` – download a previously uploaded file (requires authentication).
- `GET /archivos/sesion/<id>` – list all chat files for the given session.
- `GET /notifications` – list pending notifications for the authenticated user.
- `POST /presupuestos/generar` – send a PDF quote to a client based on item data.

**Nota:** el blueprint de autenticación se registra sin el prefijo `/auth`. Por ello las rutas anteriores se invocan directamente (por ejemplo `/login` en lugar de `/auth/login`).

## Variables de entorno

Configura `GOOGLE_OAUTH_CLIENT_ID` con el ID de cliente de tu aplicación de
Google (o varios separados por comas) para que el backend valide la audiencia
de los tokens enviados a `/google-login`.  El primer ID configurado también
puede consultarse en el endpoint `/google-client-id`, pensado para que el
frontend obtenga el valor de forma dinámica cuando sea necesario.

Si usas el frontend basado en Vite, recuerda definir `VITE_GOOGLE_CLIENT_ID` con
el mismo valor para que el botón de inicio de sesión de Google funcione
correctamente.  Vite lee las variables de entorno al compilar, por lo que debes
asegurarte de que `VITE_GOOGLE_CLIENT_ID` esté disponible en el proceso de
`npm run build`.  La falta de esta variable suele provocar errores 400 al
cargar `accounts.google.com/gsi/button`.

Recuerda también registrar la URL de tu frontend en el apartado
"Authorized JavaScript origins" de la consola de Google Cloud.
De lo contrario, el botón de inicio de sesión puede devolver errores 403.

Si el frontend falla con el mensaje `VITE_BACKEND_URL is not defined`,
asegúrate de definir esa variable con la URL del backend antes de ejecutar
`npm run dev` o `npm run build`. Vite sólo lee variables que comienzan con
`VITE_` en el entorno de compilación.

El endpoint `/api/config` permite al frontend descubrir la URL del backend y
del panel.  Para que devuelva el valor correcto, define `BACKEND_URL` con la
dirección pública de este servicio.  En Render se toma automáticamente de la
variable `RENDER_EXTERNAL_URL` cuando `BACKEND_URL` no está presente.  Las
variables `PANEL_URL` y `WIDGET_URL` se usan además para construir la lista por
defecto de orígenes permitidos en CORS.  Si el backend corre en un subdominio
(por ejemplo `https://api.ejemplo.com`), también se habilitan automáticamente
`https://ejemplo.com` y `https://www.ejemplo.com` para facilitar el uso del
widget embebido.

Adicionalmente, la variable `PUBLIC_ROOT_DOMAIN` (por defecto `chatboc.ar`)
añade `https://<dominio>` y `https://www.<dominio>` a la lista de orígenes
permitidos.  Esto permite probar en local el widget alojado en un dominio
público sin necesidad de definir manualmente `CORS_ALLOWED_ORIGINS`.

Como conveniencia adicional, los despliegues de vista previa en Vercel
(`https://*.vercel.app`) se aceptan por defecto para permitir iniciar sesión
desde versiones hospedadas en esa plataforma.

Para definir manualmente qué orígenes pueden realizar peticiones al backend,
puedes usar la variable `CORS_ALLOWED_ORIGINS` con una lista separada por comas
de URLs. Si no se especifica, se permiten los dominios definidos en `PANEL_URL`
y `WIDGET_URL` (por defecto `http://localhost:8080`), además de los dominios
derivados del `BACKEND_URL` como se describió arriba. Para aceptar peticiones
desde cualquier sitio (por ejemplo, si el widget se incrustará en múltiples
dominios), define `CORS_ALLOWED_ORIGINS=*`. El backend enviará entonces el
encabezado `Access-Control-Allow-Origin` correspondiente a cada solicitud y la
seguridad se delegará a la validación de tokens.

Define también `GOOGLE_MAPS_API_KEY` si el frontend usa el widget de mapa.
El valor se obtiene desde `/google-maps-key` para inicializar Google Maps.

`CATALOGO_RESULT_LIMIT` controla cuántos resultados devuelve por defecto el
endpoint `/catalogo/buscar` cuando no se envía `?limite=`.

Para ajustar la zona horaria de los tickets puedes definir `TIMEZONE_OFFSET` con
la diferencia respecto a UTC en horas (por ejemplo `-3` para Argentina).
Si no se indica, se asume `-3`.
Si quieres rotar frases en el globito del chat puedes definir
`ATTENTION_BUBBLE_CHOICES` con una lista separada por barras verticales
(`|`). Por ejemplo `"Hola|¿Necesitas ayuda?|¿Querés hacer un reclamo?"`.
Consulta `docs/attention-bubble.md` para más detalles sobre el endpoint
`/widget/attention` y cómo configurarlo.

## Uso correcto del rubro

El archivo `services/logic.py` define el conjunto `RUBROS_PUBLICOS` con los rubros que se tratan como entes públicos, por ejemplo `"municipio"` y `"municipios"`.

Si el rubro enviado pertenece a este conjunto, el tipo de chat correspondiente es `"municipio"`. Debe usarse el endpoint `/ask/municipio` y el campo `tipo_chat` igual a `"municipio"`. Enviar un rubro público al endpoint `/ask/pyme` resultará en un ajuste automático o un error según la configuración.

El backend prioriza siempre el rubro para elegir la lógica. Si el token o el
rubro indicado corresponde a un municipio, se utilizará la lógica de municipio
aun cuando el `tipo_chat` recibido sea "pyme". De igual manera, un rubro de pyme
forzará el uso de la lógica de pyme. No existe un tipo por defecto: si no se
puede determinar el rubro ni se envía un `tipo_chat` válido, la petición devuelve
un error claro. El cambio de lógica se registrará como mensaje informativo en los
logs, pero no interrumpe la respuesta. Para evitar el aviso simplemente envía
`tipo_chat: "municipio"` o utiliza directamente el endpoint `/ask/municipio`.

## Registro en el widget

Las funciones premium como el chat en vivo o el guardado de la ubicación requieren que el usuario esté autenticado. El registro puede hacerse sin salir del chat enviando un `POST /widget/register` con el token de la pyme o municipio en el encabezado `Authorization`. El backend asociará automáticamente al nuevo usuario con esa entidad y registrará si acepta recibir comunicaciones de marketing.

Los tickets creados desde el widget ahora se asignan al usuario final (campo `cliente_id`) en lugar de al dueño del token, permitiendo que cada ciudadano o cliente consulte luego su historial.

### Formas de enviar el token

El backend acepta el token en cualquiera de los siguientes lugares de la solicitud:

1. Encabezado `Authorization` con formato `Bearer <token>`.
2. Encabezado `X-Token`.
3. Encabezado `X-Entity-Token` (compatibilidad con versiones previas).
4. Parámetro de query string `?token=...`.
5. Campo `token` dentro del JSON o formulario enviado.

Esto permite embebidos del widget que envíen el token como atributo o en la URL sin necesidad de modificar el backend.

## Registro desde el panel

Para usuarios finales que acceden al panel web existe `POST /chatuserregisterpanel` que crea la cuenta asociada al `empresa_token` indicado. El inicio de sesión se realiza con `POST /chatuserloginpanel`. Los formularios tradicionales `/register` y `/login` son exclusivos para administradores y **no deben mostrarse** a los usuarios finales. Cuando un administrador se registra mediante `/register`, el backend crea la cuenta con el rol `admin` para que pueda gestionar empleados y clientes de inmediato.


### Migración de tickets anónimos

Si el usuario crea tickets en el widget antes de registrarse, guarda un
identificador anónimo en el navegador (`X-Anon-Id`). Los clientes legados pueden
enviar este valor usando el encabezado `Anon-Id`, pero el servidor siempre lo
responderá como `X-Anon-Id`. Al remitir ese identificador en cualquiera de estos
headers durante la llamada a `POST /widget/register`, el backend migrará
automáticamente esos tickets y comentarios para que pertenezcan al nuevo
usuario.

Para ver un ejemplo completo de cómo enviar una pregunta en modo anónimo
consulta el archivo [`docs/ejemplo-request-demo.md`](docs/ejemplo-request-demo.md).

## Mejoras recientes

- Los listados de productos del catálogo y los pedidos se ordenan por precio para que sea más fácil elegir.
- Cada artículo consolida su código, descripción y precio en una sola línea para evitar datos fragmentados.
- El historial de chat para pymes ahora conserva hasta 30 mensajes para dar más contexto en cada respuesta.
  Nuevo módulo de validaciones que comprueba nombre, correo y teléfono usando librerías open source.
- Si necesitas instalar dependencias manualmente, consulta el archivo `docs/dependencias.txt` para ver la lista completa de paquetes requeridos.
- Si tienes problemas para obtener la ubicación del usuario en el widget, revisa `docs/geolocalizacion-troubleshooting.md`.
- El valor del encabezado `Permissions-Policy` se puede personalizar con la variable de entorno `PERMISSIONS_POLICY_HEADER`.
- Para lograr una ubicacion fluida tras el registro revisa `docs/ubicacion-fluida-post-registro.md`.
- Si al cargar `window-provider.js` el navegador muestra "Invalid or unexpected token", consulta `docs/window-provider-syntaxerror.md`.
- Si al entrar a la pantalla de login la aplicación se rompe con un "Error" genérico, revisa `docs/react-login-troubleshooting.md`.
- Para más ideas orientadas a pequeñas y medianas empresas revisa `docs/ideas-pymes.md`.
- Para un ejemplo de perfil inteligente según el estado de sesión mira `docs/perfil-inteligente.md`.
- Para validar el envío de mensajes revisa `docs/whatsapp-sms-checklist.md`.
- Si quieres añadir dictado por voz en el chat consulta `docs/dictado-por-voz.md`.
- Para personalizar el globito de atención revisa `docs/attention-bubble.md`.
- Nuevo módulo `services/integracion_municipal.py` con un stub para enviar tickets a sistemas externos como SIGEM.
- Nuevo endpoint `/estadisticas/reclamos` que resume los tickets por rubro y tipo, e informa el tiempo de respuesta promedio.
- Las respuestas de agentes generan notificaciones automáticas por correo y SMS al ciudadano cuando su ticket recibe novedades.
- El analizador de sentimiento ahora responde de forma positiva o negativa para mejorar la interacción con pymes.
- Cuando el usuario solicita el PDF, el bot devuelve un enlace directo al endpoint `/catalogo/descargar` para facilitar la descarga del catálogo.
- Si el widget no aparece o surgen conflictos de integraci\u00f3n, revisa `docs/widget-troubleshooting.md` o escribe a [info@chatboc.ar](mailto:info@chatboc.ar).

## SaaS Deployment
This project can be deployed as a multi-tenant SaaS solution. Each company has its own token and context. See `docs/ARCHITECTURE.md` for how the LLM-driven flow integrates with the CRM modules.

## Session configuration
- `SESSION_COOKIE_DOMAIN`: domain to use for session cookies; leave unset for local development.
- `AUTH_TOKEN_COOKIE_NAME`: name of the fallback cookie storing the user's auth token when headers are missing.

## New Features
- The LLM now asks to confirm stored addresses before searching.
- Queries like "veterinarias" reset any ongoing complaint context and clear previous complaint details.
- Tool executions log parameters and show a fallback message when no results are found.

## Log Utilities
You can inspect log files using `script_filter_logs.py`.
This helper allows filtering by log level, searching with a regular expression
and limiting the output to the last N lines. Example:

```bash
python script_filter_logs.py logs/chatbot.log --level ERROR --contains reclamo --tail 50
```
