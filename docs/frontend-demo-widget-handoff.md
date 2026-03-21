# Frontend Handoff – Demo Widget UX (Public Landing)

## Objetivo
Garantizar que en `https://www.chatboc.ar` el primer contacto del usuario siempre abra selector de rubro/demo y nunca entre directo al flujo `municipio` por default.

## Backend ya cubre
- En requests públicos anónimos (`Origin` o `Referer` de chatboc.ar), el backend prioriza selector de demo en primer turno.
- Se evita 403 por límite de plan del owner durante descubrimiento público.

## Tareas frontend recomendadas

### 1) Payload de inicialización estable
En el primer `POST /api/ask/{tipo}` enviar siempre:
- `pregunta: "__INIT__"`
- `tenant_slug=municipio` (o el que usen para bootstrap)
- `X-Chat-Session-Id` estable por sesión del browser

> Aunque backend ya tolera vacío, esto evita ambigüedades.

### 2) Preservar headers en todos los turnos
Asegurar que **cada** request de chat incluya:
- `X-Chat-Session-Id`
- `X-Token` (si widget usa token estático de entidad)
- `Content-Type: application/json`

### 3) Consumir respuesta tipo selector
Cuando llegue respuesta con:
- `fuente: "demo_selector"`
- `message_type: "interactive_list"` o `interactive_buttons`
- `options_list` o `botones`

renderizar lista/botones y reenviar click como:
- `action_id` del botón (`demo_select_rubro:<key>`)

### 3.1) Consumir `ux_context` para pintar la UI correcta
Cada respuesta del chat puede incluir:
- `ux_context.trusted_owner`
- `ux_context.owner_tipo_chat`
- `ux_context.owner_name`
- `ux_context.should_render_demo_shell`

Regla recomendada:
- si `trusted_owner=true` y `should_render_demo_shell=false`, renderizar experiencia tenant real y **no** volver a mostrar shell/showroom de demo;
- si `trusted_owner=false`, sí mantener la experiencia demo pública.

### 4) Evitar doble init por race conditions
Si socket + HTTP disparan saludo inicial duplicado:
- usar flag `initSent` en cliente
- bloquear segundo `__INIT__` hasta recibir primera respuesta

### 5) Telemetría de onboarding
Registrar eventos:
- `widget_opened`
- `demo_selector_rendered`
- `demo_option_clicked` (con `demo_key`)
- `first_real_question_sent`
- `lead_cta_clicked`

### 5.1) Telemetría de continuidad tenant
Registrar además:
- `tenant_context_restored`
- `tenant_context_lost`
- `demo_shell_render_blocked`

Esto ayuda a detectar cuándo el frontend dejó de reenviar `entityToken`, `anon_id` o `pin`.

## Criterios de aceptación UX
- Abrir widget en home pública => aparece selector de rubros.
- Elegir rubro => arranca demo específica (no municipio genérico).
- No aparece 403 en primeros mensajes de demo pública.
- Si refresca página, conserva sesión y estado de demo.


### 6) Flujo de captación de lead (nuevo)
Cuando usuario pulse botón `open_demo_form`:
- enviar `action_id: "open_demo_form"`.
- backend responde `fuente: "demo_lead_capture"` y `pedir_info` en secuencia: `nombre -> telefono -> email`.
- UI debe renderizar input libre + botones sugeridos.
- al finalizar, mostrar código de seguimiento (`#<nro_ticket>`) al usuario.

### 7) Mapeo de botones críticos
- `open_demo_form`: inicia captura de lead prospecto.
- `demo_menu:home`: vuelve a acciones de demo.
- `demo_menu:back`: vuelve a selector de rubros.
