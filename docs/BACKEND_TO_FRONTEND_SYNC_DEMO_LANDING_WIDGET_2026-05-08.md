# Backend to Frontend Sync - Demo, Landing y Widget 2026-05-08

Objetivo: que la primera experiencia de Chatboc deje de ser una grilla pasiva y pase a ser un recorrido vendible: elegir pilar, elegir demo, abrir panel demo, probar widget/chat/WhatsApp y convertir en lead/ticket/pedido/caso escolar.

## Problemas vistos en produccion

- `/api/v2/demo/session` devolvia 400 cuando frontend mandaba solo el pilar o una categoria visual sin `rubro`/`tenant_slug`.
- El selector de demos mostraba solo dos pilares: Empresas y Sector Publico. Falta Colegios.
- Los botones de demos no navegan a una experiencia usable.
- `/api/public/realtime/voice-capabilities` y `/public/realtime/voice-capabilities` daban 404/CORS en el backend desplegado actual.
- El widget intenta conectar Socket.IO en `https://www.chatboc.ar/socket.io`, pero el backend usa `/api/socket.io`.
- La landing tiene buena base, pero falta narrativa visual, panel demo real y flujo de conversion.

## Backend implementado

### 1. Pilares demo canonicos

Contrato nuevo:

```json
{
  "contract_version": "demo.catalog.v2",
  "pillar_contract_version": "demo.pillars.v1",
  "sectors": ["educacion", "gobierno", "empresas"],
  "pillars": [],
  "sector_groups": []
}
```

Pilares:

- `educacion`: label `Colegios`, default rubro `colegios`.
- `gobierno`: label `Gobiernos`, default rubro `municipio`.
- `empresas`: label `Empresas`, default rubro `local_comercial_general`.

Endpoint:

```txt
GET /api/v2/demo/catalog
```

### 2. Demo session tolerante

`POST /api/v2/demo/session` ahora acepta:

```json
{ "sector": "colegios" }
```

```json
{ "sector": "Soluciones para Empresas" }
```

```json
{ "pillar": "educacion", "category_slug": "colegios" }
```

```json
{ "sector": "empresas", "rubro_slug": "ferreteria" }
```

Si falta `rubro` y `tenant_slug`, backend usa el default del pilar. No debe quedar "Cargando demos" por 400.

### 3. Recursos demo

Cada pilar principal devuelve PDFs demo:

- `/media/demo_catalogs/colegios/catalogo-demo-colegios.pdf`
- `/media/demo_catalogs/colegios/lista-precios-demo.pdf`
- `/media/demo_catalogs/gobiernos/catalogo-demo-gobiernos.pdf`
- `/media/demo_catalogs/gobiernos/lista-precios-demo.pdf`
- `/media/demo_catalogs/empresas/catalogo-demo-empresas.pdf`
- `/media/demo_catalogs/empresas/lista-precios-demo.pdf`

El frontend debe renderizarlos como acciones del demo: "Ver catalogo", "Descargar lista", "Enviar al chat".

### 4. Rubros legacy tambien recibe Colegios

`GET /rubros/?format=tree` y `GET /api/rubros/?format=tree` ahora deben mostrar tres raices demo cuando `ENABLE_DEMO_MODE=true`:

- Soluciones para Empresas
- Soluciones para Sector Publico
- Colegios e instituciones educativas

### 5. Realtime voice aliases

Backend agrega alias para:

```txt
GET /api/public/realtime/voice-capabilities
GET /public/realtime/voice-capabilities
GET /realtime/voice-capabilities
OPTIONS en las mismas rutas
```

Frontend debe usar la canonica:

```txt
GET {BACKEND_URL}/api/public/realtime/voice-capabilities
```

No usar same-origin `/api/public/...` desde `www.chatboc.ar` salvo que exista rewrite/proxy real.

## Tareas frontend urgentes

### Landing

- Reemplazar "Cargando demos" infinito por estados `loading`, `error`, `empty`, `ready`.
- Si falla `/api/v2/demo/catalog`, mostrar fallback local minimo con tres pilares, pero con banner "modo demo local".
- Renderizar tres tabs visibles: `Colegios`, `Gobiernos`, `Empresas`.
- Cada tab debe mostrar categorias/cards con CTA primaria `Probar demo`.
- El CTA debe llamar `POST /api/v2/demo/session` y navegar a `/demo?session={demo_session_id}` o abrir el panel demo embebido.

### Demo panel

Al elegir demo, mostrar una pantalla demo completa con:

- Header del pilar y rubro elegido.
- Chat real usando `chat_bootstrap.endpoint`, headers y payload del backend.
- Cards de `workspace.value_cards`.
- Recursos de `workspace.catalog_resources`.
- Acciones de `workspace.conversion_ctas.actions`.
- Samples de `workspace.sample_conversations` que puedan prefillear el composer.
- Composer multimodal segun `workspace.media_capabilities`.
- Estado/analytics mini demo: tickets, encuestas, leads, satisfaccion.

### Widget

- Al abrir por primera vez, preguntar:
  1. Colegios
  2. Gobiernos
  3. Empresas
- Despues pedir categoria/rubro.
- Despues iniciar conversacion con `POST /api/v2/demo/session`.
- No hardcodear botones sin accion. Cada boton debe tener `intent`, `payload` o `href`.
- Para Socket.IO usar backend origin y path:

```txt
origin: https://chatbot-backend-2e14.onrender.com
path: /api/socket.io
```

No usar:

```txt
https://www.chatboc.ar/socket.io
```

### UX/UI premium esperado

- No landing "texto + cards" solamente. Necesitamos experiencia de producto viva.
- Hero con panel operativo real visible y selector demo integrado.
- Animaciones suaves en selector, chat, carga de audio, imagen, ubicacion, ticket creado y lead capturado.
- Botones con iconos, estados disabled/loading/success/error.
- El demo debe sentirse como una app operativa, no como una pagina informativa.
- En mobile, el widget no debe tapar CTA primarias ni quedar cortado con DevTools/viewport chico.

## Encuestas, votaciones y comentarios

Frontend debe tratar estas superficies como demo y como tenant full:

- Demo: renderizar modulos mock/contract-driven desde el backend cuando no haya tenant pago.
- Tenant full: consumir endpoints reales de encuestas, votaciones, comentarios y analytics.
- Siempre mostrar estados UX claros: sin datos, cargando, error con `request_id`, publicado/no publicado, borrador, cerrado.

Pendiente backend para siguiente ola:

- Unificar contrato publico de demo para encuestas/votaciones/comentarios.
- Agregar `workspace.modules.surveys`, `workspace.modules.voting`, `workspace.modules.comments`.
- Exponer ejemplos de analytics demo para esos modulos.

## Regla de sincronizacion

Frontend no debe inventar labels ni categorias. Si necesita algo para la experiencia, pedir campo al backend. Backend ya entrega `pillars`, `sector_groups`, `workspace`, `chat_bootstrap`, `catalog_resources`, `media_capabilities`, `conversion_ctas` y `animation_tokens`.
