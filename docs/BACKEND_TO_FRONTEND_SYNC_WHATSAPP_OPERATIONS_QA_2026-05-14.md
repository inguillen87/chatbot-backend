# Backend to Frontend Sync - WhatsApp Operations QA - 2026-05-14

## Objetivo

Frontend debe mostrar una experiencia operativa premium sin inventar datos. Backend ya valida y persiste los casos de prueba principales por WhatsApp:

- Municipio Junin: reclamo por texto con foto.
- Municipio Junin: reclamo con ubicacion compartida.
- Municipio Junin: reclamo desde audio transcripto.
- Pyme Cuatro Fincas: pedido de vinos con total y tracking.

## Contratos a consumir

### Reclamo municipal

Cuando el backend devuelve o expone un ticket municipal, frontend puede renderizar solo datos reales:

- `nro_ticket`
- `categoria`
- `direccion`
- `latitud`
- `longitud`
- `nombre_vecino`
- `telefono_vecino`
- `canal_ingreso`
- `foto_url_directa`
- cantidad/listado de `archivos`

Reglas de render:

- Mostrar mapa solo si existen `latitud` y `longitud`.
- Mostrar evidencia/foto solo si existe `foto_url_directa` o adjuntos asociados.
- No mostrar marcador, ruta, heatmap, SLA ni progreso inventado.
- Si `canal_ingreso=whatsapp`, rotular como caso ingresado por WhatsApp.
- En detalle de ticket, mostrar comentarios de sistema con `archivo_adjunto_id` cuando existan.

### Pedido PYME

Cuando el backend devuelve o expone un pedido PYME, frontend puede renderizar:

- `nro_pedido`
- `nombre_cliente`
- `telefono_cliente`
- `monto_total`
- `detalles[]`
- URL de tracking `/tracking/order/{nro_pedido}`

Reglas de render:

- Renderizar lineas desde `detalles[]`, no parsear texto del mensaje.
- No usar `Vecino/a` como nombre si backend manda un nombre real.
- Mostrar total desde `monto_total`.
- Mostrar tracking solo si viene `nro_pedido`.
- No inventar stock, envio, descuentos ni fechas si backend no los envia.

## QA backend verificada

Simulacion Twilio webhook firmada contra backend:

- 15/15 requests respondieron `200`.
- Delta creado: `3` tickets municipales, `1` pedido PYME, `2` adjuntos.
- Reclamo texto+foto creo ticket con `foto_url_directa` y `archivos=1`.
- Reclamo ubicacion creo ticket con `latitud` y `longitud`.
- Pedido Cuatro Fincas persistio cliente real `QA Bodega`, telefono, total y lineas:
  - `2 x MALBEC`
  - `1 x CABERNET SAUVIGNON`

## Tareas frontend

- En pantalla de demo/chat, usar los datos devueltos por backend para mostrar resultado operativo despues de crear ticket/pedido.
- En admin/inbox, mostrar evidencia adjunta cuando `archivos > 0` o `foto_url_directa` exista.
- En reclamo con ubicacion, mostrar mapa solo cuando haya coordenadas reales.
- En pedido PYME, renderizar resumen desde `detalles[]` y tracking desde `/tracking/order/{nro_pedido}`.
- En estados vacios, no mostrar mapas, heatmaps, metricas ni cards si backend no envia datos.
- En errores de runtime, mostrar mensaje accionable desde JSON backend; no mostrar stack traces ni texto tecnico.

## No hacer en frontend

- No crear tickets, pedidos, metricas, empleados, mapas ni estados falsos.
- No usar PDFs como accion principal de demo.
- No mostrar placeholders de imagen si la URL no carga.
- No mezclar portal usuario final dentro del panel admin.
