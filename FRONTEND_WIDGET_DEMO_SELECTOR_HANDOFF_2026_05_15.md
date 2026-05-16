# Frontend Widget Demo Selector Handoff - 2026-05-15

## Objetivo

Que una persona pueda abrir el widget publico de Chatboc, elegir Colegios, Gobiernos o Empresas, y entrar a una demo real sin falso error rojo ni mezcla de tenant.

El backend no define layout. Entrega estado, sesion, endpoints y capacidades. El frontend define UX/UI, transiciones, selector, estados visuales y render del chat.

## Problema observado

En produccion se ve:

- El usuario toca `Gobiernos`.
- `POST /api/v2/demo/session` responde `200`.
- El widget muestra `No pudimos iniciar esta demo real. Reintenta en unos minutos.`

Eso indica que frontend esta tratando como error una respuesta HTTP correcta, probablemente por contrato ambiguo, falta de `ok/session/status` o payload demasiado pesado para el flujo de selector.

Los logs `lockdown-install.js`, `overlay.js No matching tab found`, errores de wallets/extensiones y el mensaje PWA `beforeinstallprompt` no deben mostrarse como error Chatboc. Son ruido del navegador/extensiones o estado PWA.

## Backend disponible ahora

`POST /api/v2/demo/session` acepta labels visuales y modo compacto.

Tambien acepta textos de CTA o cards como `Probar colegio`, `Probar gobierno`, `Ver catalogo`, `Bodega`, `Ferreteria`, `Municipio`, etc. Backend intenta inferir `sector` por rubro/texto, pero frontend debe mandar `sector` cuando lo tenga.

Payload recomendado desde el widget:

```json
{
  "surface": "widget",
  "source": "landing_widget_selector",
  "sector": "gobierno",
  "label": "Gobiernos",
  "anon_id": "anon-id-si-existe",
  "chat_session_id": "chat-session-id-si-existe"
}
```

Para Colegios:

```json
{
  "surface": "widget",
  "source": "landing_widget_selector",
  "sector": "educacion",
  "label": "Colegios"
}
```

Para Empresas:

```json
{
  "surface": "widget",
  "source": "landing_widget_selector",
  "sector": "empresas",
  "label": "Empresas"
}
```

Empresas ahora es un flujo de dos pasos cuando no viene rubro explicito. Si el usuario toca `Empresas`, frontend debe renderizar `workspace.rubro_selector.categories` y no abrir chat todavia.

Respuesta esperada para Empresas sin rubro:

```json
{
  "ok": true,
  "ready": true,
  "status": "ready",
  "next_step": "select_rubro",
  "requires_rubro_selection": true,
  "workspace": {
    "rubro_selector": {
      "contract_version": "demo.rubro_selector.v1",
      "render_as": "rubro_selector",
      "sector": "empresas",
      "categories": []
    }
  },
  "widget_onboarding": {
    "status": "select_rubro",
    "open_chat": false,
    "close_selector": false,
    "send_init_once": false,
    "rubro_selector": {}
  },
  "frontend_contract": {
    "render_as": "demo_rubro_selector",
    "next_step": "select_rubro",
    "rubro_selector_path": "workspace.rubro_selector"
  }
}
```

Cuando el usuario elige rubro dentro de Empresas, enviar otra vez `POST /api/v2/demo/session` con rubro explicito:

```json
{
  "surface": "widget",
  "source": "landing_widget_rubro_selector",
  "sector": "empresas",
  "rubro": "ferreteria",
  "label": "Ferreteria"
}
```

Para bodega:

```json
{
  "surface": "widget",
  "source": "landing_widget_rubro_selector",
  "sector": "empresas",
  "rubro": "bodega",
  "label": "Bodega"
}
```

Regla: no pasar `tenant_slug` para Empresas desde el selector general. Pasar `rubro`. Backend resuelve tenant demo y publica `workspace.rubro_context` para que el chat no responda como rubro equivocado.

Respuesta OK esperada:

```json
{
  "contract_version": "demo.session.v2",
  "ok": true,
  "ready": true,
  "status": "ready",
  "response_profile": "widget_compact",
  "demo_session_id": "...",
  "chat_session_id": "sid-or-uuid",
  "session": {
    "demo_session_id": "...",
    "chat_session_id": "sid-or-uuid",
    "tenant_slug": "municipio",
    "sector": "gobierno",
    "rubro": "municipio"
  },
  "workspace": {
    "chat_bootstrap": {}
  },
  "widget_onboarding": {
    "status": "ready",
    "open_chat": true,
    "close_selector": true,
    "send_init_once": true,
    "chat_bootstrap_path": "workspace.chat_bootstrap",
    "default_menu": {}
  }
}
```

## Regla de exito frontend

Considerar la demo iniciada si:

- HTTP status es `2xx`.
- `payload.ok !== false`.
- Existe `payload.session.chat_session_id` o `payload.chat_session_id`.
- Existe `payload.workspace.chat_bootstrap` o `payload.chat_bootstrap`.

No mostrar el error rojo si la respuesta cumple esas condiciones, aunque falte algun campo secundario.

Excepcion: si `payload.next_step === "select_rubro"` o `payload.requires_rubro_selection === true`, no debe existir todavia `session` ni `chat_bootstrap`. Eso no es error: frontend debe renderizar `workspace.rubro_selector.categories`.

## Menu predeterminado por rubro

La demo session publica:

- `workspace.default_menu`
- `workspace.quick_menu`
- `chat_bootstrap.default_menu`
- `widget_onboarding.default_menu`
- `workspace.rubro_context`
- `workspace.rubro_tools`
- `chat_bootstrap.payload.demo_metadata`

Regla frontend:

- Renderizar primero `workspace.default_menu.items`.
- Si no existe, usar `workspace.quick_menu`.
- Si no existe, usar `workspace.quick_replies`.
- No mostrar un widget vacio despues de iniciar demo.
- No usar el menu de Junin si el usuario acaba de elegir Colegio o Empresa.
- Si `workspace.rubro_context.slug === "bodega"`, mostrar copy y sugerencias de bodega.
- Si `workspace.rubro_context.slug === "ferreteria"`, mostrar copy y sugerencias de ferreteria.
- No reutilizar textos de bodega para ferreteria ni viceversa.

## Herramientas del rubro

Backend publica:

- `workspace.rubro_tools.contract_version === "demo.rubro_tools.v1"`
- `workspace.rubro_tools.enabled_tools`
- `workspace.rubro_tools.resources`
- `workspace.rubro_tools.price_resources`
- `workspace.rubro_tools.locations`
- `workspace.rubro_tools.contact`
- `workspace.rubro_tools.hours`
- `workspace.rubro_tools.faq_preview`

Render frontend recomendado:

- Mostrar una bandeja compacta de herramientas despues del saludo/menu inicial.
- Fuente unica: `workspace.rubro_tools.enabled_tools`.
- Ocultar toda herramienta con `enabled !== true`.
- `catalog`: abrir recursos desde `items[].url`.
- `price_list`: abrir recursos desde `items[].url`, etiquetar como precios/stock/lista.
- `location`: si `items[].maps_url` existe, abrir Google Maps. No construir maps URL en frontend si backend no la manda.
- `contact`: mostrar telefono, WhatsApp, email o web solo si estan en `data`.
- `hours`: mostrar horarios solo si `data` existe.
- `faq`: renderizar preguntas frecuentes desde `items`.

No inventar:

- precios,
- ubicaciones,
- telefonos,
- horarios,
- FAQs,
- catalogos.

Si el usuario pide "ubicacion", "telefono", "horarios", "catalogo" o "lista de precios" y la herramienta existe, frontend puede responder con la tarjeta/herramienta local antes o junto al mensaje del chat. Si no existe, debe dejar que el runtime responda o mostrar estado vacio: `Dato no publicado para este rubro`.

Ejemplo:

```json
{
  "default_menu": {
    "contract_version": "demo.default_menu.v1",
    "render_as": "quick_menu",
    "selected_sector": "educacion",
    "items": [
      { "id": "menu_asistencia", "label": "Asistencia", "intent": "justificar_inasistencia" }
    ]
  }
}
```

## Regla de tenant

Cuando el widget esta en modo selector publico (`landing_widget_selector`):

- No reutilizar `tenant_slug=junin-1` solo porque el usuario esta logueado o la pagina tiene tenant previo.
- No pasar `tenant_slug` salvo que el usuario haya elegido explicitamente probar ese tenant real.
- Si se elige `Gobiernos`, dejar que backend resuelva el tenant demo o pasar `tenant_slug: "municipio"`.
- Si se elige `Colegios`, dejar que backend resuelva colegio demo o pasar `tenant_slug: "colegio-demo"`.
- Si se elige `Empresas`, dejar que backend resuelva pyme demo o pasar `tenant_slug: "bodega"` o el rubro elegido.

Esto evita que `Colegios` termine usando un municipio real o que `Empresas` herede contexto de Junin.

Si el CTA sale desde una card de rubro, enviar tambien:

```json
{
  "surface": "widget",
  "source": "landing_widget_selector",
  "label": "Probar colegio",
  "sector": "educacion",
  "rubro": "colegios"
}
```

Si frontend no sabe el sector, puede mandar solo `label`, pero debe aceptar la sesion que backend resuelva.

## Uso obligatorio de chat_bootstrap

Despues de iniciar demo:

1. Leer `const bootstrap = payload.workspace?.chat_bootstrap ?? payload.chat_bootstrap`.
2. Guardar `payload.session.demo_session_id` y `payload.session.chat_session_id`.
3. Usar `bootstrap.same_origin_endpoint ?? bootstrap.endpoint`.
4. Enviar headers de `bootstrap.headers`.
5. Enviar query de `bootstrap.query`.
6. Enviar body base de `bootstrap.payload`.

No inferir endpoint localmente por sector.

## Evitar resets y duplicados

- Enviar `__INIT__` una sola vez por `chat_session_id`.
- Si el usuario reabre widget con la misma sesion, no volver a mandar `__INIT__` automaticamente.
- No disparar `POST /api/v2/demo/session` varias veces por hover, rerender o cambio de tema.
- Mientras inicia, bloquear botones de sector y mostrar estado `Iniciando demo...`.

## UX/UI esperado

- Primer estado: una sola pregunta clara: `Que tipo de organizacion queres simular?`
- Tres opciones grandes: `Colegios`, `Gobiernos`, `Empresas`.
- Al elegir, cerrar selector y abrir chat si `widget_onboarding.open_chat === true`.
- Mostrar un loader corto dentro del widget, no una alerta roja.
- El error rojo solo aparece si backend devuelve `ok: false`, status no 2xx, o no hay session/chat_bootstrap.
- En error, mostrar texto humano y `request_id` en modo debug/copiable.
- Mantener composer limpio: texto + iconos para adjuntar, ubicacion, audio y emoji.
- No llenar header con botones de carrito, audio, usuario, catalogo y llamada a la vez.

## Multimodal

Usar `workspace.media_capabilities` y `chat_bootstrap.supports`:

- `text`: enviar `pregunta`.
- `image/file`: subir adjunto y enviar `attachmentInfo`.
- `audio`: multipart con `audio_file`.
- `location`: enviar `{ lat, lng, address, accuracy }`.
- `emoji`: texto normal.

Ocultar capacidades no soportadas por browser o backend. No mostrar botones rotos.

## QA minima frontend

- Abrir `chatboc.ar` limpio, sin tenant, elegir Colegios: debe abrir chat demo colegio.
- Elegir Gobiernos: debe abrir chat demo municipio.
- Elegir Empresas: debe mostrar selector de rubro, no abrir bodega directo.
- Elegir Empresas > Bodega: debe abrir chat con menu de vinos, cajas y pedidos.
- Elegir Empresas > Ferreteria: debe abrir chat con menu de productos, calculo de materiales y presupuesto.
- Desde card/CTA `Probar colegio`: debe abrir colegio, no Junin.
- Desde cualquier rubro con `label` o `rubro`: debe abrir chat con `workspace.default_menu.items`.
- Estando logueado como Junin, el selector publico no debe forzar Junin para Colegios/Empresas.
- Si `POST /api/v2/demo/session` devuelve `200 ok=true`, no debe mostrarse `No pudimos iniciar`.
- `__INIT__` no debe repetirse mas de una vez por sesion.
- El widget debe conservar `X-Chat-Session-Id` y `X-Demo-Session-Id`.
- Los errores de extensiones del navegador no deben aparecer como errores Chatboc.
