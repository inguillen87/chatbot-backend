# Frontend Widget Menus Runtime Contract

Fecha: 2026-05-17

## Objetivo

El widget web y el sandbox WhatsApp deben usar los menus publicados por backend para empresas, gobiernos y colegios. Frontend no debe inventar textos, rutas, acciones, disponibilidad humana, montos, catalogos ni resultados operativos.

## Endpoint de chat

Usar siempre `workspace.chat_bootstrap.endpoint` o `chat_bootstrap.endpoint`.

Endpoints soportados:

- `/ask/pyme`
- `/ask/municipio`
- `/api/ask`
- `/api/ask/pyme`
- `/api/ask/municipio`

No usar fallback a `/ask` en `www.chatboc.ar` si el contrato trae `/api/ask/pyme` o `/ask/pyme`. No probar bases alternativas despues de una respuesta de limite.

## Request para acciones de menu

Cada click de menu se envia como POST al endpoint de `chat_bootstrap`:

```json
{
  "pregunta": "",
  "tenant_slug": "qa-colegio-sandbox",
  "tipo_chat": "pyme",
  "demo_mode": true,
  "action_id": "create_school_case",
  "education_context": { "is_education": true }
}
```

Headers obligatorios si existen en contrato:

```json
{
  "X-Chat-Session-Id": "sid_...",
  "X-Demo-Session-Id": "eyJ...",
  "X-Tenant-Slug": "qa-colegio-sandbox",
  "X-Anon-Id": "anon..."
}
```

## Lectura de mensajes

Orden recomendado:

1. `messages[].content`
2. `assistant_message.content`
3. `message_body`
4. `respuesta`
5. `respuesta_usuario`
6. `message`

Si backend responde 200 y `messages[]` esta vacio, mostrar `message_body` y registrar warning compacto. No descartar botones por falta de `messages[]`.

## Lectura de menus

Leer acciones desde:

1. `workspace.education.primary_actions`
2. `workspace.education.quick_menu`
3. `workspace.default_menu.items`
4. `workspace.quick_menu`
5. `quick_menu`
6. `botones`
7. `options_list`
8. `experience_blueprint.conversion_ctas.actions`

Para cada item usar:

- label: `label`, `texto`, `title`, `cta_label`
- action id: `action_id`, `intent`, `action`, `id`, `key`
- descripcion: `description`, `descripcion`, `detail`

Deduplicar por `action_id` o `intent`. Renderizar solo `enabled !== false`.

## URLs y recursos

Si un item trae `url`, abrir como link externo:

```tsx
<a href={resource.url} target="_blank" rel="noreferrer">
```

No convertir `/api/v2/demo/catalog-assets/...` ni `/media/demo_catalogs/...` en rutas SPA. No enviar un PDF como mensaje de chat.

## Limites de demo

Si backend responde:

- `reason_code: "demo_message_limit_reached"`
- `reason_code: "anonymous_trial_limit_reached"`
- `reason_code: "anonymous_message_limit_reached"`

Frontend debe:

- bloquear composer y botones operativos,
- mantener historial visible,
- mostrar card comercial desde `upgrade`,
- no reintentar en `/ask`, `/api/ask/pyme` ni otra base,
- mostrar `request_id` solo como trazabilidad compacta.

## Comportamiento por rubro

### Colegios

Acciones principales:

- `create_school_case`
- `justify_absence`
- `talk_secretary`

`talk_secretary` puede devolver:

```json
{
  "success": true,
  "request_id": "req_...",
  "fuente": "education_widget_live_handoff",
  "data": {
    "ticket_id": 123,
    "chat_id": "P-123456",
    "status": "esperando_agente_en_vivo",
    "live_chat": {},
    "school_case": {}
  }
}
```

Frontend no inventa horarios ni disponibilidad si `data.live_chat` no lo publica.

### Gobiernos

Acciones minimas:

- `iniciar_reclamo`
- `info_tramite`
- `consultar_estado`
- `human_handoff`

Frontend renderiza el menu y envia `action_id`. La creacion de reclamos, estado y derivacion se decide por backend.

### Empresas

Acciones minimas:

- `ver_catalogo`
- `crear_pedido`
- `preparar_checkout`
- `derivar_humano`

Si backend responde `amount_validated: false`, frontend no muestra monto final confirmado.

## QA frontend

1. Cambiar Empresas -> Colegios -> Gobiernos no conserva `tenant_slug`, `widget_token` ni `chat_bootstrap` anterior.
2. Todo click de menu envia `action_id`.
3. Ningun click cae a `/ask` si `chat_bootstrap.endpoint` existe.
4. Limite de demo no dispara retry.
5. `messages[]`, `message_body` y botones se renderizan aunque uno de los campos venga ausente.
6. PDFs abren desde `/api/v2/demo/catalog-assets/...` sin pasar por React Router.
7. Colegios no muestra copy municipal.
8. Gobiernos no muestra copy comercial.
9. Empresas no muestra monto final si no fue validado por backend.
10. Widget mantiene navegacion por teclado y lector de pantalla.
