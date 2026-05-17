# Frontend to Backend - Widget Menu Runtime Hotfix

Fecha: 2026-05-17

## Objetivo

El widget web y el sandbox WhatsApp deben mostrar y ejecutar menus de rubro, acciones rapidas y flujos escolares, pyme y municipio dentro del chat.

Reglas base:

- Frontend no inventa textos, rutas, acciones, disponibilidad humana, montos, catalogos ni resultados operativos.
- Frontend no navega a rutas internas para ejecutar acciones operativas.
- Frontend no mezcla `tenant_slug`, `widget_token`, `chat_bootstrap` ni owner entre rubros.
- Backend debe devolver siempre algun contenido renderizable o un error JSON estable con `request_id`.

## 1. Chat bootstrap siempre accionable

`POST /api/v2/demo/session` debe publicar `workspace.chat_bootstrap` y, por compatibilidad, puede publicarlo tambien en `chat_bootstrap`.

Ejemplo:

```json
{
  "workspace": {
    "chat_bootstrap": {
      "contract_version": "demo.chat_bootstrap.v1",
      "endpoint": "/api/ask/pyme",
      "same_origin_endpoint": "/api/ask/pyme",
      "fallback_endpoint": null,
      "method": "POST",
      "query": {
        "tenant_slug": "qa-colegio-sandbox",
        "tenant": "qa-colegio-sandbox"
      },
      "headers": {
        "X-Tenant-Slug": "qa-colegio-sandbox",
        "X-Chat-Session-Id": "sid_...",
        "X-Demo-Session-Id": "demo_...",
        "X-Anon-Id": "anon_..."
      },
      "payload": {
        "tenant_slug": "qa-colegio-sandbox",
        "tenant": "qa-colegio-sandbox",
        "tipo_chat": "pyme",
        "demo_mode": true
      }
    }
  }
}
```

Reglas:

- No publicar `/ask/...` como endpoint visual o publico si no acepta `POST` en produccion.
- `endpoint` y `same_origin_endpoint` deben ser `/api/ask`, `/api/ask/pyme` o `/api/ask/municipio`.
- `fallback_endpoint` debe ser `null` o una ruta `/api/ask/...`; nunca `/ask`.
- `X-Demo-Session-Id` identifica la demo.
- `X-Chat-Session-Id` identifica la conversacion.
- Frontend debe usar el endpoint publicado. No debe probar bases alternativas despues de una respuesta de limite.

## 2. Request para acciones de menu

Cada click de menu se envia como `POST` al endpoint de `chat_bootstrap`.

Ejemplo:

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

Headers esperados cuando existen en contrato:

```json
{
  "X-Chat-Session-Id": "sid_...",
  "X-Demo-Session-Id": "demo_...",
  "X-Tenant-Slug": "qa-colegio-sandbox",
  "X-Anon-Id": "anon_..."
}
```

Regla backend importante:

- `pregunta=""` con `action_id` no es una solicitud de menu.
- Solo tratar como menu inicial cuando `action_id` este ausente y `pregunta` sea `""` o `"__INIT__"`, o cuando `action_id` sea `menu`, `menu_principal`, `menu_colegio` o `main_menu`.

## 3. Toda accion debe devolver contenido renderizable

Cuando frontend envia una accion como `create_school_case`, `crear_reclamo`, `crear_pedido`, `cotizar_envio`, `talk_secretary`, `derivar_humano` o similar, backend debe responder con al menos uno de estos bloques:

- `message`, `respuesta`, `texto`, `message_to_user`, `message_body` o `respuesta_usuario`.
- `messages[]`, `assistant_message.content` o `chat_messages[]`.
- `botones`, `buttons` o `quick_replies`.
- `interactive_sections` o `interactive_list.sections`.
- `confirmation_card`.
- `data` con resultado operativo real: `ticket_id`, `chat_id`, `status`, `school_case`, `order`, `claim`, `lead` o `handoff`.

Ejemplo minimo:

```json
{
  "success": true,
  "request_id": "req_...",
  "message": "Decime que paso y, si queres, adjunta una foto o PDF.",
  "botones": [
    { "texto": "Hablar con secretaria", "action_id": "talk_secretary" }
  ],
  "data": {
    "status": "esperando_detalle",
    "school_case": {}
  }
}
```

Reglas frontend:

- No descartar botones por falta de `messages[]`.
- Si backend responde 200 sin texto pero con `data`, renderizar resultado operativo.
- Si backend responde 200 sin ningun bloque renderizable, mostrar estado vacio controlado y loggear warning compacto.

## 4. Lectura de mensajes

Orden recomendado:

1. `messages[].content`
2. `assistant_message.content`
3. `chat_messages[].content`
4. `message_body`
5. `message_to_user`
6. `respuesta`
7. `respuesta_usuario`
8. `texto`
9. `message`

## 5. Contrato de menu corto por rubro

Backend debe publicar menus accionables desde alguno de estos paths:

1. `workspace.default_menu.items`
2. `workspace.quick_menu`
3. `workspace.primary_actions`
4. `workspace.government.primary_actions`
5. `workspace.government.quick_menu`
6. `workspace.gobierno.primary_actions`
7. `workspace.gobierno.quick_menu`
8. `workspace.municipio.primary_actions`
9. `workspace.municipio.quick_menu`
10. `workspace.education.primary_actions`
11. `workspace.education.quick_menu`
12. `workspace.education_profile.primary_actions`
13. `workspace.education_profile.quick_menu`
14. `workspace.pyme.primary_actions`
15. `workspace.pyme.quick_menu`
16. `workspace.business.primary_actions`
17. `workspace.business.quick_menu`
18. `workspace.commerce.primary_actions`
19. `workspace.commerce.quick_menu`
20. `workspace.operational_menu.primary_actions`
21. `workspace.operational_menu.quick_menu`
22. `workspace.verticals.<active_vertical>.actions`
23. `experience_blueprint.conversion_ctas.actions`

Formato recomendado:

```json
{
  "label": "Crear caso escolar",
  "description": "Conta que paso y adjunta foto, audio o PDF si hace falta.",
  "action_id": "create_school_case",
  "payload": {
    "education_context": { "is_education": true }
  }
}
```

Aliases frontend soportados:

- label: `label`, `texto`, `title`, `cta_label`.
- action id: `action_id`, `intent`, `action`, `id`, `key`.
- descripcion: `description`, `descripcion`, `detail`.

Reglas:

- Deduplicar por `action_id`, `intent`, `action`, `id`, `key`, `label` o `title`.
- Renderizar solo `enabled !== false`.
- Menus operativos no deben traer `url` salvo que sean PDF, Excel, catalogo descargable o recurso externo.
- `label`, `description`, `action_id` y `payload` salen del backend.
- El menu corto debe tener 3 a 5 acciones principales.
- El resto puede venir como `interactive_sections` despues de que el usuario pida mas opciones.
- Si backend conoce el nombre del visitante o contacto, puede publicar saludo personalizado.
- Si no lo conoce, backend debe pedirlo dentro del flujo. Frontend no lo inventa.

## 6. Gobiernos y municipios

Acciones esperadas cuando el tenant las soporte:

```json
[
  {
    "label": "Crear reclamo",
    "description": "Contame que paso. Podes adjuntar foto, audio o ubicacion.",
    "action_id": "crear_reclamo",
    "payload": { "vertical": "municipio" }
  },
  {
    "label": "Consultar estado",
    "description": "Busca un reclamo por numero o datos de contacto.",
    "action_id": "consultar_estado_reclamo"
  },
  {
    "label": "Consultar tramite",
    "description": "Responde desde tramites publicados por el municipio.",
    "action_id": "consultar_tramite"
  },
  {
    "label": "Hablar con una persona",
    "description": "Deriva a mesa de atencion si esta disponible.",
    "action_id": "derivar_humano"
  }
]
```

Reglas:

- Foto, audio y ubicacion son entradas del chat, no paginas separadas.
- Confirmacion visual de reclamo requiere `ticket_id`, `chat_id`, `status` o comprobante real en `data` o `confirmation_card`.
- No mostrar mapa si backend no publica coordenadas, direccion, `geo_layers` o pedido explicito de GPS.
- No usar copy comercial si el rubro es gobierno.

## 7. Colegios

Acciones esperadas cuando el tenant las soporte:

```json
[
  {
    "label": "Crear caso escolar",
    "description": "Conta que paso y adjunta foto, audio o PDF si hace falta.",
    "action_id": "create_school_case",
    "payload": { "education_context": { "is_education": true } }
  },
  {
    "label": "Justificar inasistencia",
    "description": "Carga alumno, curso, fecha, motivo y certificado si existe.",
    "action_id": "justify_absence"
  },
  {
    "label": "Hablar con secretaria",
    "description": "Crea espera para secretaria o muestra horario real.",
    "action_id": "talk_secretary"
  }
]
```

Respuesta minima soportada para `talk_secretary`:

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

Reglas:

- Usar lenguaje escolar solo si backend lo publica: familia, alumno, curso, secretaria, inasistencia, certificado, comunicado.
- No usar textos municipales: reclamos urbanos, turnos municipales, vecino, tramites express municipales.
- `registrar_intencion_pago_colegio` no es pago realizado. Backend debe devolver estado y texto claro de intencion o checkout si aplica.
- Adjuntos escolares se tratan como evidencia de caso escolar, no como catalogo comercial.
- Frontend no inventa horarios ni disponibilidad si `data.live_chat` no lo publica.

## 8. Pymes, empresas y rubros comerciales

Acciones esperadas cuando el tenant las soporte:

```json
[
  {
    "label": "Consultar producto",
    "description": "Busca precio, disponibilidad o detalle publicado por el comercio.",
    "action_id": "consultar_producto"
  },
  {
    "label": "Crear pedido",
    "description": "Pide datos y confirma el pedido solo con respuesta del backend.",
    "action_id": "crear_pedido"
  },
  {
    "label": "Cotizar envio",
    "description": "Usa direccion o ubicacion enviada por el usuario.",
    "action_id": "cotizar_envio"
  },
  {
    "label": "Hablar con ventas",
    "description": "Deriva o registra lead comercial.",
    "action_id": "capturar_lead_comercial"
  }
]
```

Reglas:

- Catalogos, PDFs, listas de precio, Excel y promos descargables pueden abrirse fuera del chat.
- Pedido, checkout, cotizacion, contacto, imagen para reconocer producto y ubicacion deben continuar dentro del chat.
- Frontend no calcula totales, envio, stock ni precio final.
- Si backend responde `amount_validated: false`, frontend no muestra monto final confirmado.
- Pedido confirmado requiere validacion backend en el ultimo paso.

## 9. Rubros nuevos o genericos

Cuando `active_vertical=general` o el rubro no sea municipio, pyme o colegio, backend debe publicar acciones genericas si quiere que el widget opere.

Ejemplo:

```json
[
  {
    "label": "Registrar consulta",
    "description": "Deja una solicitud trazable para el equipo.",
    "action_id": "registrar_solicitud_operativa",
    "payload": { "tipo_solicitud": "consulta" }
  },
  {
    "label": "Hablar con una persona",
    "description": "Pide datos de contacto y deriva al equipo.",
    "action_id": "derivar_humano"
  }
]
```

Regla:

- Frontend no inventa certificados, boletas, pagos, turnos, precios ni resoluciones para rubros genericos.

## 10. Herramientas de rubro

`workspace.rubro_tools.enabled_tools` debe diferenciar herramientas descargables y herramientas operativas dentro del chat.

Descargables:

- `catalog`
- `price_list`
- `pdf`
- `excel`
- `resource`
- items con `url` o `action_url`

Operativas dentro del chat:

- `contact`
- `location`
- `hours`
- `faq`
- `create_order`
- `create_claim`
- `school_case`
- `human_handoff`
- items con `action_id`

Ejemplo:

```json
{
  "id": "faq",
  "kind": "rubro_tool",
  "label": "Consultas frecuentes",
  "description": "Preguntas frecuentes publicadas por el colegio.",
  "enabled": true,
  "action_label": "Consultar",
  "action_id": "faq"
}
```

Reglas:

- Recursos con `url` se abren como link externo.
- Recursos operativos con `action_id` se ejecutan dentro del chat.
- No convertir `/api/v2/demo/catalog-assets/...` ni `/media/demo_catalogs/...` en rutas SPA.
- No enviar un PDF como mensaje de chat.

Ejemplo frontend para recursos:

```tsx
<a href={resource.url} target="_blank" rel="noreferrer">
```

## 11. Errores y limites de demo

Si la demo queda limitada, backend debe responder JSON estable:

```json
{
  "ok": false,
  "reason_code": "demo_message_limit_reached",
  "message": "Llegaste al limite de mensajes gratis de esta demo.",
  "request_id": "req_...",
  "trial_usage": {
    "channel": "chat",
    "limit": 5,
    "used": 5,
    "remaining": 0
  },
  "upgrade": {
    "title": "Ya viste la demo real. Sigamos con una prueba guiada.",
    "cta_label": "Dejar datos",
    "lead_capture_endpoint": "/api/public/lead-capture"
  }
}
```

Reason codes esperados:

- `demo_message_limit_reached`
- `anonymous_trial_limit_reached`
- `anonymous_message_limit_reached`

Reglas:

- `403` solo debe usarse para token invalido, tenant bloqueado o limite real.
- Si hay `403`, incluir siempre `reason_code`, `message` y `request_id`.
- No devolver HTML ni stack traces.
- Frontend bloquea composer y botones operativos.
- Frontend mantiene historial visible.
- Frontend muestra card comercial desde `upgrade`.
- Frontend no reintenta en `/ask`, `/api/ask/pyme` ni otra base.
- Frontend muestra `request_id` solo como trazabilidad compacta.

## 12. Marketplace e inventario Pro

Documento completo: `docs/FRONTEND_TO_BACKEND_MARKETPLACE_INVENTORY_PRO_2026_05_17.md`.

Reglas para demos:

- `demo_mode=true` no confirma stock real, inventario, precio final ni disponibilidad comercial.
- El catalogo demo puede mostrar recursos ilustrativos y descargables.
- Botones de pedido en demo muestran el flujo, pero la confirmacion comercial real queda deshabilitada o marcada como demo.

Reglas para tenants pagos:

- Admin, widget, WhatsApp, chat profesional, carrito y pedidos usan el mismo catalogo backend.
- Frontend renderiza `stock`, `stock_quantity`, `stock_status`, `available_to_sell`, `inventory`, `catalog_version` y `request_id`.
- Frontend no calcula stock ni precio final.
- Pedido confirmado requiere validacion backend en el ultimo paso.

Endpoints a consumir:

- `GET /api/admin/tenants/{tenant_slug}/catalog`
- `GET /api/admin/tenants/{tenant_slug}/catalog/items`
- `PATCH /api/admin/tenants/{tenant_slug}/catalog/items/{item_id}`
- `POST /api/admin/catalog/import`
- `GET /api/admin/catalog/import/{upload_id}`
- `PUT /api/admin/catalog/import/{upload_id}`
- `POST /api/admin/catalog/import/{upload_id}/commit`
- `GET /api/v2/tenants/{tenant_slug}/catalog/quality`

Importacion:

- `mode=upsert`: actualiza por SKU y crea faltantes.
- `mode=replace`: reemplaza catalogo del tenant.
- `mode=stock_only`: actualiza solo stock por SKU y no crea productos nuevos.

## 13. Accesibilidad minima del widget

Base recomendada: WCAG 2.2 AA. El widget debe tratar chat, voz y avatar como superficies accesibles, no como decoracion.

Requisitos minimos:

- El widget abierto debe tener `role="dialog"` o region equivalente, titulo accesible y cierre con `Escape`.
- Foco atrapado dentro del widget mientras esta abierto; al cerrar, vuelve al boton que lo abrio.
- Todos los botones iconicos tienen `aria-label`.
- Mensajes nuevos anuncian cambios con `aria-live="polite"`.
- Estados de carga usan `aria-busy`; botones bloqueados usan `aria-disabled`.
- Navegacion 100% por teclado: abrir, cerrar, escribir, enviar, adjuntar, menu tres puntos y elegir accion.
- Avatar/realtime con controles visibles: pausar animacion, silenciar, activar subtitulos/transcripcion y repetir ultimo mensaje.
- Respetar `prefers-reduced-motion`.
- Contraste suficiente en modo claro y oscuro; foco visible y no solo por color.
- Inputs con etiquetas reales, no solo placeholders.
- Adjuntos y ubicacion tienen descripciones accesibles.

Ayuda contextual sugerida:

```txt
Chatboc tambien esta pensado para personas que no pueden o no quieren escribir. Podes usar voz, subtitulos, lectura, adjuntos y derivacion humana para comunicarte con una institucion o empresa sin quedar afuera.
```

No mostrarla como modal invasivo. Usar tooltip o ayuda contextual cerca del boton de voz/avatar y una entrada clara en el menu de accesibilidad.

## 14. QA compartida

1. Abrir `/demo?sector=gobierno`, elegir municipio y abrir widget.
2. Ver menu corto municipal con acciones reales publicadas por backend.
3. `Crear reclamo` hace `POST /api/ask/municipio` o endpoint publicado por `chat_bootstrap`, no `/ask`.
4. Foto, audio y ubicacion para reclamo siguen dentro del chat y muestran confirmacion solo con ticket real.
5. Abrir `/demo?sector=educacion`, elegir colegio y abrir widget.
6. Ver menu corto escolar con acciones reales.
7. `Crear caso escolar` hace `POST /api/ask/pyme`, no `/ask`.
8. `Justificar inasistencia` acepta texto, audio, imagen o PDF dentro del chat.
9. `Hablar con secretaria` devuelve `status`, `live_chat` y `ticket_id` o `chat_id` cuando cree espera real.
10. Abrir `/demo?sector=empresas`, elegir bodega, ferreteria o almacen y abrir widget.
11. `Consultar producto`, `Crear pedido` y `Cotizar envio` operan dentro del chat.
12. Catalogos y listas descargables abren como link externo.
13. La respuesta de cualquier accion muestra mensaje, card, botones o resultado operativo, no warning de payload sin mensajes.
14. Agotar limite de demo muestra card comercial, no error rojo ni reintento automatico.
15. Cambiar Empresas -> Colegios -> Gobiernos no conserva `tenant_slug`, `widget_token` ni `chat_bootstrap` anterior.
16. Colegios no muestra copy municipal.
17. Gobiernos no muestra copy comercial.
18. Empresas no muestra monto final si no fue validado por backend.
19. PDFs demo abren desde `/api/v2/demo/catalog-assets/...` sin pasar por React Router.
20. Widget se usa completo con teclado y lector de pantalla.
21. Tenant pago puede actualizar stock por `PATCH` y por import `stock_only`.
22. Widget/WhatsApp no confirma compra si `stock_status` es `stock_unknown` u `out_of_stock`.
