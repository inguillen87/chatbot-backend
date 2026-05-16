# Backend / Frontend Demo UX Handoff - 2026-05-15

## Objetivo

Separar responsabilidades para que el usuario final tenga una demo fluida, real y vendible:

- Backend entrega contratos, sesiones, rubros, acciones, datos trazables y errores JSON.
- Frontend entrega UX/UI, selector, transiciones, estados visuales, mobile, accesibilidad y feedback.
- Ningun equipo inventa lo que le corresponde al otro.

## Regla Principal

Frontend no debe inventar resultados operativos.

Backend no debe decidir layout, animaciones ni estilo visual.

Si backend no entrega `session`, `chat_bootstrap`, `rubro_selector`, `default_menu`, `rubro_context` o `request_id`, frontend debe usar estado vacio o error controlado. No debe fabricar bodega, ferreteria, colegio, ticket, pedido, encuesta ni mapa.

## Flujo Publico Correcto

### Paso 1: Selector de sector

El widget publico muestra:

- Colegios
- Gobiernos
- Empresas

Payload recomendado:

```json
{
  "surface": "widget",
  "source": "landing_widget_selector",
  "sector": "empresas",
  "label": "Empresas"
}
```

### Paso 2: Si es Empresas, elegir rubro

Backend responde:

```json
{
  "ok": true,
  "status": "ready",
  "next_step": "select_rubro",
  "requires_rubro_selection": true,
  "workspace": {
    "rubro_selector": {
      "contract_version": "demo.rubro_selector.v1",
      "categories": []
    }
  },
  "widget_onboarding": {
    "status": "select_rubro",
    "open_chat": false,
    "send_init_once": false
  }
}
```

Esto no es error. Frontend debe renderizar rubros.

Payload al elegir rubro:

```json
{
  "surface": "widget",
  "source": "landing_widget_rubro_selector",
  "sector": "empresas",
  "rubro": "ferreteria",
  "label": "Ferreteria"
}
```

### Paso 3: Abrir chat

Solo abrir chat cuando backend responda:

- `ok !== false`
- `widget_onboarding.open_chat === true`
- existe `workspace.chat_bootstrap`
- existe `session.chat_session_id`

Frontend debe usar `workspace.chat_bootstrap`, no inferir endpoints.

## Responsabilidad Backend

Backend debe mantener:

- `POST /api/v2/demo/session` estable para `gobierno`, `educacion`, `empresas`.
- Empresas sin rubro: devolver `rubro_selector`, no crear session operativa.
- Empresas con rubro: devolver `session`, `chat_bootstrap`, `default_menu`, `rubro_context`.
- `rubro_context` distinto por rubro:
  - `bodega`: vinos, cajas, maridajes, promociones, pedidos.
  - `ferreteria`: herramientas, materiales, calculos, presupuestos, stock.
  - generico: catalogo, pedido, stock, derivacion.
- `chat_bootstrap.payload.demo_metadata` para que `/ask/pyme` persista contexto de rubro.
- `workspace.rubro_tools` para que cada rubro publique herramientas operativas:
  - catalogo y recursos descargables,
  - lista de precios o stock cuando exista,
  - ubicacion con `maps_url` de Google Maps cuando haya direccion/coordenadas,
  - telefono/contacto/web cuando este configurado,
  - horarios cuando esten publicados,
  - FAQ del rubro cuando exista archivo trazable.
- Errores JSON con `request_id`.
- Tests de contrato para selector, bodega, ferreteria y runtime.

Backend no debe:

- Publicar metricas falsas.
- Abrir bodega como default cuando el usuario solo eligio Empresas.
- Crear `local_comercial_general` por el primer click de Empresas.
- Mandar HTML a frontend.
- Devolver `500` por input esperable.

## Responsabilidad Frontend

Frontend debe implementar:

- Selector sectorial claro dentro del widget.
- Si `next_step=select_rubro`, render de `workspace.rubro_selector.categories`.
- Al elegir rubro, nuevo `POST /api/v2/demo/session` con `rubro`.
- Loader corto mientras inicializa.
- Error rojo solo si:
  - HTTP no es `2xx`, o
  - `ok === false`, o
  - se esperaba chat pero falta `session/chat_bootstrap`.
- Estado especial para selector de rubro: no pedir `session/chat_bootstrap`.
- Guardar `X-Chat-Session-Id` y `X-Demo-Session-Id`.
- Enviar `__INIT__` una sola vez por `chat_session_id`.
- Renderizar `workspace.default_menu.items` como menu inicial.
- Usar `workspace.rubro_context` para copy visual y microcopy.
- Renderizar `workspace.rubro_tools.enabled_tools` como bandeja de herramientas del rubro.
- Si una herramienta viene deshabilitada, ocultarla. No completar con datos falsos.
- Si `location.items[].maps_url` existe, abrir Google Maps desde ese link.
- Si `catalog` o `price_list` trae recursos, abrirlos/descargarlos usando `url`.

Frontend no debe:

- Reutilizar `tenant_slug=junin-1` en selector publico.
- Abrir bodega si el usuario solo eligio Empresas.
- Mostrar menu de bodega para ferreteria.
- Inventar ubicacion, telefono, horarios, precios o FAQ si `workspace.rubro_tools` no lo entrega.
- Mostrar error por logs de extensiones del navegador.
- Mostrar carrito en municipio si no corresponde.
- Iniciar multiples sesiones por rerender.

## Herramientas Por Rubro

Contrato nuevo:

```json
{
  "contract_version": "demo.rubro_tools.v1",
  "sector": "empresas",
  "rubro": "ferreteria",
  "tenant_slug": "ferreteria",
  "tools": [],
  "enabled_tools": [],
  "resources": [],
  "price_resources": [],
  "locations": [],
  "contact": {},
  "hours": null,
  "faq_preview": [],
  "frontend_contract": {
    "render_as": "tool_tray",
    "source_path": "workspace.rubro_tools.enabled_tools",
    "hide_disabled_tools": true,
    "open_maps_with": "items[].maps_url",
    "do_not_invent_missing_tools": true
  }
}
```

Reglas:

- Backend solo publica una herramienta como `enabled=true` si tiene datos reales/configurados.
- `catalog` usa recursos de catalogo demo y/o `data/pyme/rubros/<rubro>/config.json`.
- `price_list` se habilita si hay recursos de precios, stock, Excel o lista.
- `location` se habilita si hay direccion/coordenadas y siempre entrega `maps_url`.
- `faq` se habilita desde `faq.json`, no desde texto inventado.
- `workspace.rubro_tools` lleva el contrato completo para frontend.
- `chat_bootstrap.payload.rubro_tool_summary` y `demo_metadata.tool_summary` llevan un resumen liviano para que el runtime pueda responder sobre ubicacion, horarios, catalogo y FAQ sin inflar el payload compacto.

## UX/UI Requerido

### Widget

- Primer estado: una pregunta simple.
- Botones grandes por sector.
- Empresas abre grilla/lista de rubros.
- Cada rubro debe tener titulo corto y dos señales:
  - que puede probar,
  - que dato real se genera.
- Una vez iniciado chat, mostrar:
  - mensaje inicial,
  - menu rapido,
  - adjuntos disponibles,
  - estado de sesion.

### Pyme

Para bodega:

- Ver vinos
- Armar caja
- Sugerir maridaje
- Pedido mayorista

Para ferreteria:

- Buscar producto
- Calcular materiales
- Armar presupuesto
- Coordinar envio

### Gobierno

- Crear reclamo
- Consultar estado
- Enviar ubicacion
- Adjuntar foto/audio
- Ver seguimiento

### Colegio

- Justificar inasistencia
- Consultar admisiones
- Enviar certificado
- Responder encuesta/votacion

## QA Compartida

Cada deploy debe probar:

1. Home limpia, no logueado.
2. Widget -> Gobiernos -> abre municipio.
3. Widget -> Colegios -> abre colegio.
4. Widget -> Empresas -> muestra rubros.
5. Empresas -> Bodega -> menu de vinos.
6. Empresas -> Ferreteria -> menu de herramientas/materiales.
7. El primer click de Empresas no crea session ni chat.
8. Al elegir rubro se crea `chat_session_id`.
9. `__INIT__` no se duplica.
10. El chat conserva headers de bootstrap.
11. Adjuntos respetan `media_capabilities`.
12. Error tecnico muestra `request_id`.

## Contratos Backend Ya Disponibles

- `demo.rubro_selector.v1`
- `demo.rubro_context.v1`
- `demo.chat_metadata.v1`
- `demo.default_menu.v1`
- `demo.session_frontend.v1`
- `demo.widget_onboarding_result.v1`

## Tests Backend Verificados

```txt
test_v2_demo_session_empresas_sector_only_returns_rubro_selector
test_v2_demo_session_pyme_rubro_profiles_are_distinct
test_ask_pyme_demo_persists_rubro_metadata_from_chat_bootstrap
```

Tambien paso:

```txt
pytest tests/test_api_v2_foundation.py tests/test_public_resolver_widget_config_contract.py tests/test_public_resolver_quick_menu.py -q
scripts/local_platform_smoke.py
```

## Resultado Esperado

El usuario puede probar:

- un municipio,
- un colegio,
- una bodega,
- una ferreteria,
- otra pyme generica,

sin que el frontend invente datos y sin que backend mezcle rubros. Cada chat debe tener menu, contexto, sesion, headers y contrato propio.
