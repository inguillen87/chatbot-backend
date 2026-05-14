# Frontend Handoff - Widget, Portal Ciudadano y Seguimiento Junin - 2026-05-14

## Objetivo

Renderizar una experiencia publica premium para municipio Junin sin inventar datos:

- Widget embebible en una pagina externa.
- Chat primero, acciones compactas despues.
- Portal ciudadano accesible como anonimo.
- Registro progresivo sin perder reclamos, carrito, historial ni encuestas.
- Seguimiento de reclamos, pedidos, canjes y puntos con datos reales del backend.

## Contratos backend listos

### Conversion completa: landing -> demo -> lead -> tenant -> WhatsApp

Usar como mapa principal de integracion para que la experiencia sea simple desde el primer contacto:

```txt
GET /api/public/landing-experience
```

Campos nuevos/relevantes:

```json
{
  "conversion": {
    "lead_capture": {
      "contract_version": "public.lead_capture.form.v1",
      "fields": [],
      "required_fields": ["name"],
      "required_any_of": [["phone", "email"]],
      "progressive_capture": {},
      "success_state": {},
      "validation_state": {}
    },
    "journey": {
      "contract_version": "public.conversion_journey.v1",
      "steps": [],
      "frontend_rules": {}
    }
  }
}
```

Reglas frontend:

- Renderizar landing, demo y widget desde `conversion.journey`, no desde rutas hardcodeadas dispersas.
- Persistir `anon_id`, `demo_session_id` y `chat_session_id` durante todo el recorrido.
- No enviar `POST /api/public/lead-capture` hasta tener `name` y `phone` o `email`.
- Prefillear el formulario con datos ya capturados por chat cuando existan.
- Si el usuario quiere crear/registrar tenant y aun no hay self-service completo, enviar lead con `source=tenant_signup_interest` e `interest=crear_tenant`.
- Despues de crear tenant, mostrar integracion WhatsApp desde `GET /api/v2/tenants/:tenant_slug/whatsapp/tech-provider`.
- No mostrar pasos de Twilio Console, ni inventar estados de activacion. Usar el contrato Tech Provider.
- Success de lead usa `frontend_contract.render_as=lead_capture_success`.
- Errores de validacion usan `frontend_contract.render_as=lead_capture_validation`.
- En el superadmin/ventas, usar `next_actions`: abrir lead, WhatsApp, email y llamada solo si el canal requerido existe.

### Onboarding WhatsApp Tech Provider

Usar cuando un tenant quiera activar WhatsApp productivo sin abrir Twilio Console. El cliente debe ver solo Chatboc y un flujo guiado dentro del panel.

```txt
GET /api/v2/tenants/:tenant_slug/whatsapp/tech-provider
POST /api/v2/tenants/:tenant_slug/whatsapp/tech-provider/provision
POST /api/v2/tenants/:tenant_slug/whatsapp/tech-provider/embedded-signup
```

Respuesta principal:

```json
{
  "contract_version": "twilio.tech_provider.v1",
  "provider": "twilio_tech_provider",
  "automation": {
    "mode": "api_first",
    "manual_twilio_console_allowed": false,
    "customer_sees_twilio_console": false,
    "env": {
      "ready": true,
      "missing": []
    }
  },
  "embedded_signup": {
    "enabled": true,
    "required_customer_action": "login_with_facebook_embedded_signup",
    "completion_endpoint": "/api/v2/tenants/municipio/whatsapp/tech-provider/embedded-signup"
  },
  "api_workflow": [],
  "frontend_contract": {
    "render_as": "twilio_tech_provider_onboarding",
    "show_twilio_brand": false,
    "show_manual_console_steps": false,
    "primary_action": "start_embedded_signup",
    "show_phone_choice": true,
    "show_progress_steps": true
  },
  "limitations": []
}
```

Reglas frontend:

- Renderizar como onboarding Chatboc, no como tutorial de Twilio.
- No mostrar links a Twilio Console, logos de Twilio ni pasos manuales de consola.
- Mostrar progreso desde `api_workflow`: subcuenta, messaging service, embedded signup, sender y estado.
- Si `automation.env.ready=false`, mostrar configuracion pendiente para superadmin/plataforma, no para el cliente final.
- `POST /provision` prepara/persiste plan en dry-run si live mode esta apagado; no debe prometer activacion productiva.
- `POST /embedded-signup` se llama cuando Meta Embedded Signup devuelve `waba_id`, `phone_number_id` y `session_id`.
- Mostrar `limitations` como mensajes humanos: login Meta embebido, OTP del numero y aprobaciones Meta. No decir que Chatboc puede saltear esos pasos.
- Si Meta/Twilio muestran un aviso obligatorio dentro del popup de Embedded Signup, no ocultarlo ni taparlo; fuera de ese popup la experiencia debe seguir siendo Chatboc.
- Despues de `next_action=register_whatsapp_sender_via_senders_api`, frontend debe quedar en estado "activacion en proceso" hasta que backend publique sender listo.

### Sandbox WhatsApp demo sin login

Usar cuando una persona entra a probar Chatboc sin usuario ni contrasena y quiere elegir rubro desde una botonera:

```txt
GET /api/v2/demo/whatsapp-sandbox?sector=empresas&rubro=bodega
POST /api/v2/demo/whatsapp-sandbox
```

Payload opcional:

```json
{
  "sector": "gobierno|empresas|educacion",
  "rubro": "bodega|ferreteria|colegio|municipio",
  "tenant_slug": "opcional",
  "source": "public_demo_profile"
}
```

Respuesta principal:

```json
{
  "contract_version": "demo.whatsapp_sandbox_launcher.v1",
  "requires_auth": false,
  "session": {
    "demo_session_id": "token-largo",
    "chat_session_id": "sid_corto",
    "max_messages": 10
  },
  "whatsapp_sandbox": {
    "contract_version": "demo.whatsapp_sandbox.v1",
    "sandbox": {
      "display_number": "+1 (415) 523-8886",
      "join_phrase": "join ... o null si es numero dedicado",
      "activation_message": "Mensaje inicial para abrir WhatsApp",
      "requires_join_phrase": true,
      "wa_deeplink": "https://wa.me/...",
      "qr_url": "https://..."
    },
    "trial_policy": {
      "max_messages": 10,
      "free_inputs": ["text", "image", "audio", "location", "file"]
    },
    "scenario_scripts": [],
    "catalog": {},
    "surveys_votings": {}
  }
}
```

Reglas frontend:

- No pedir login para mostrar este launcher.
- Renderizar selector de sector/rubro desde `whatsapp_sandbox.rubro_options`.
- Mostrar QR/deeplink/frase de union desde `whatsapp_sandbox.sandbox`.
- Si `requires_join_phrase=false`, no mostrar paso de union a sandbox: abrir directo con `activation_message`.
- Mostrar contador de 10 mensajes desde `trial_policy.max_messages`.
- Mostrar scripts sugeridos desde `scenario_scripts`, sin simular resultados.
- Mostrar catalogo PDF/Excel solo si `catalog.resources` o `catalog.pdf_excel_upload_demo.enabled` viene del backend.
- Mostrar entrada a encuestas/votaciones solo si `surveys_votings.enabled=true`.
- No inventar tickets, pedidos, casos escolares, votos, precios ni metricas.

### Widget externo

Usar:

```txt
GET /api/public/tenants/municipio/widget-config?tenant_slug=municipio
GET /api/public/widget-commerce-session?tenant_slug=municipio
GET /api/public/widget-user/tenant-history?tenant_slug=municipio
POST /api/public/widget-user/register?tenant_slug=municipio
POST /api/public/widget-user/link-session?tenant_slug=municipio
GET /api/pwa/public/cart/summary?tenant=municipio
```

Headers que frontend debe mantener estables:

```txt
X-Chat-Session-Id: <chat_session_id corto>
X-Anon-Id: <anon_id persistente>
Origin: <host externo real>
```

Reglas:

- Persistir `anon_id` en localStorage/cookie y reenviarlo en todos los endpoints publicos.
- Persistir `chat_session_id` por conversacion/widget.
- No usar `demo_session_id` como `chat_session_id`.
- Si backend devuelve `session.chat_session_id` con `sid_...`, usarlo desde ese momento.

### Registro del usuario del widget

`POST /api/public/widget-user/register` ahora requiere:

```json
{
  "name": "Vecino Junin",
  "phone": "+549261...",
  "email": "opcional@example.com"
}
```

Respuesta OK trae:

- `profile.user_id`
- `tenant_follow.linked`
- `merge.municipio_tickets`
- `merge.market_carts`
- `merge.chat_contexts`
- `portal.view_url`

Si falta contacto:

- `reason_code=validation_failed`
- `required_fields=["name","email_or_phone"]`
- `field_errors`

Si el email ya existe:

- `status=verification_required`
- frontend debe pedir login/verificacion, no asumir que la cuenta quedo vinculada.

## UX requerida frontend

### Widget

- Primer nivel: conversacion real.
- Segundo nivel: botones compactos para historial, carrito/canjes, portal y WhatsApp.
- No mostrar carrito si `cart.enabled=false`.
- No mostrar catalogo/canjes si `catalog.enabled=false`.
- No mostrar PDFs como accion principal.
- No mostrar botones duplicados o ajenos al flujo.
- Composer debe soportar texto, imagen, audio y ubicacion si backend/config lo habilita.

### Portal anonimo

`/portal/:tenant` debe abrir aunque no haya login:

- Mostrar historial desde `tenant-history` usando `X-Anon-Id` y `X-Chat-Session-Id`.
- Mostrar reclamos, pedidos, mensajes, encuestas y cart si existen.
- Mostrar CTA de registro progresivo cuando `profile.can_register=true`.
- Despues de registrarse, llamar `link-session` y refrescar `tenant-history`.

### Seguimiento de reclamos

Para cada item `kind=claim` usar `detail_endpoint`.

Render esperado:

- Estado actual con chip claro.
- Timeline de eventos si backend lo envia.
- Adjuntos si existen.
- Mapa solo si hay lat/lng reales.
- CTA para agregar comentario/foto solo si backend publica endpoint.
- No inventar tiempos de reparacion ni empleados asignados.

### Canjes y puntos

- Si cart trae items con puntos, mostrarlo como beneficios/canjes, no como ecommerce generico.
- Mostrar balance de puntos solo si backend lo envia.
- Si falta identidad para canjear, pedir registro o telefono/email.
- No inventar puntos, ranking ni premios.

## Pagina externa de prueba

Backend dejo una pagina de contrato:

```txt
docs/widget_junin_external_contract_test.html
```

Uso local recomendado:

```powershell
cd docs
python -m http.server 8765
```

Abrir:

```txt
http://127.0.0.1:8765/widget_junin_external_contract_test.html
```

Configurar `API base` a:

```txt
http://127.0.0.1:5000
```

Esa pagina no reemplaza el widget final: solo valida contratos, CORS, sesiones, registro, historial y carrito desde un origin externo.

## Checklist frontend

- Widget externo no rompe estilos del host.
- No hay overflow mobile.
- Dark/light mode conserva contraste.
- La sesion anonima sobrevive refresh.
- Registro no borra historial.
- Portal no muestra admin ni configuracion interna.
- Seguimiento usa solo `detail_endpoint` y datos reales.
- Errores JSON se muestran como estados humanos, no como stack/HTML.
- Si un endpoint devuelve `reserved_public_slug`, no reintentar en loop.
- Si `registration_required`, abrir formulario minimo: nombre + telefono/email.

## Update backend demo municipio - 2026-05-14

Backend agrego runtime demo especifico para gobierno/municipio en `POST /ask/municipio`.

### Lo que frontend debe mandar

- Mantener `X-Chat-Session-Id` desde `workspace.chat_bootstrap.headers`.
- Mantener `demo_session_id` en query/header/payload.
- Enviar texto en `pregunta`.
- Enviar ubicacion como:

```json
{
  "location": {
    "lat": -34.61,
    "lng": -58.44,
    "address": "Av. San Martin 123"
  }
}
```

- Enviar adjuntos como `attachmentInfo` con `id`, `url`, `mimeType` y `name`.
- Para audio o video, usar el mismo contrato de adjuntos si ya existe archivo subido; si es multipart de audio, backend transcribe y procesa el texto.

### Respuesta esperada

Cuando el usuario pide baches, alumbrado, semaforo, residuos, reclamo, manda foto/audio/video/archivo o comparte ubicacion, backend responde `chat.response.v1` con:

- `fuente: "demo_municipio_runtime"`.
- `ticket` con `id`, `nro_ticket`, `consulta_pin`, `status`, `category`, `lat`, `lng`, `detail_endpoint`.
- `result.kind: "ticket"` y `result.traceable: true`.
- `actions[0]` con `creates: "ticket"` y campos operativos.
- `media_understanding.supports`: text, image, audio, video, file, location, emoji, voice_call, video_call.

Frontend no debe inventar respuesta local si no llega esto: mostrar error humano o `empty_states.runtime_unavailable`.

### Admin preview

`GET /api/v2/demo/admin-preview?sector=gobierno&tenant_slug=municipio` ahora refleja tickets reales creados por el runtime demo:

- `cards[0].value`: reclamos reales creados.
- `cards[1].value`: ubicaciones reales capturadas.
- `map.enabled=true` solo si hay lat/lng reales.
- `session_activity.has_session_data=true` solo si existe actividad real.

### UX pendiente frontend

- Mostrar el ticket creado como resultado principal, no como boton suelto.
- Cuando `media_understanding.received` incluya `image`, `audio`, `video`, `file` o `location`, mostrar chips/adjuntos compactos.
- Si `ticket.detail_endpoint` existe, el CTA debe ser "Ver seguimiento" o equivalente.
- Pasar `chat_session_id` tambien al admin preview cuando frontend lo tenga: `/api/v2/demo/admin-preview?sector=gobierno&tenant_slug=municipio&chat_session_id=...`.
- No volver a mostrar "Crear ticket" debajo de un ticket ya creado; usar "Adjuntar evidencia", "Enviar ubicacion", "Consultar estado".

## Update QA backend - 2026-05-14

Backend dejo dos recorridos repetibles para que frontend valide sin mocks:

```powershell
.\test_venv\Scripts\python.exe scripts\local_platform_smoke.py
```

Ese smoke valida landing/demo/widget/admin con DB in-memory. Caso clave para frontend:

- Crea demo session gobierno.
- Envia `POST /api/ask/municipio` con `X-Chat-Session-Id`, `demo_session_id`, ubicacion y foto.
- Recibe `fuente=demo_municipio_runtime`, `ticket`, `result.traceable=true`.
- Consulta `GET /api/v2/demo/admin-preview?...&chat_session_id=...`.
- Admin preview devuelve `cards`, `map.points` y `session_activity.items` reales de esa sesion.

```powershell
$env:TWILIO_AUTH_TOKEN='local-token'
$env:TWILIO_ACCOUNT_SID='AC_LOCAL_TEST'
.\test_venv\Scripts\python.exe scripts\qa_whatsapp_flows.py
```

Ese QA simula WhatsApp con fake Twilio, fake storage y fake transcripcion. No toca Twilio real ni DB real por defecto.

Flujos cubiertos:

- Junin reclamo por texto, imagen, ubicacion y audio.
- Cuatro Fincas pedido de vinos y confirmacion.
- Colegio sandbox: menu, justificar inasistencia, audio/ubicacion.

### Tareas frontend que salen de esta QA

- Agregar un recorrido E2E visual que reproduzca el caso `demo_gobierno_chat_creates_traceable_claim`.
- En `/demo`, preservar el `chat_session_id` de `workspace.chat_bootstrap.session.chat_session_id` durante toda la conversacion.
- Al pedir admin preview, pasar el mismo `chat_session_id`; si no, el panel queda en cero por diseno.
- En integraciones/WhatsApp sandbox, mostrar que el modo de prueba es `copy_or_deeplink` y no prometer envio automatico desde backend.
- Para WhatsApp Tech Provider, renderizar el contrato como onboarding Chatboc; no mostrar Twilio Console al usuario final.
- En perfil/integracion, diferenciar:
  - sandbox demo: prueba guiada con 10 mensajes y numeros de prueba;
  - produccion: onboarding Tech Provider/Embedded Signup y estado de provisioning.
- Para adjuntos de WhatsApp/widget, mostrar estados compactos: recibido, transcripto, asociado a ticket/pedido/caso.
- Para colegio, si backend devuelve `education_context` o `school_case_alias`, mostrar seguimiento escolar y no mezclarlo con ecommerce.
