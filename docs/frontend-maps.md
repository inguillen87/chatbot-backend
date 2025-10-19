# Directrices de mapas y estadísticas para el frontend

Para que el equipo de frontend implemente los mapas y las estadísticas solicitadas, utilizar los siguientes endpoints del backend:

- `GET /estadisticas/mapa_calor/datos` devuelve puntos geojson para construir un mapa de calor de tickets. Admite parámetros opcionales como `tipo_ticket`, `municipio_id`, `rubro_id`, `fecha_inicio`, `fecha_fin`, `categoria` y `estado`. El payload expone `map_layers.heatmap` y `map_config` para indicar que el formato preferido es GeoJSON y que el proveedor recomendado es MapLibre/MapTiler. El formato específico para Google (`heatmap_google`) quedó **obsoleto** y ya no se envía porque Google discontinuó la capa nativa de heatmap.
- `GET /estadisticas/usuarios/ubicaciones` entrega una lista de objetos `{lat, lng}` con las ubicaciones de los usuarios del mismo municipio.
- `GET /tickets/municipio/<id>/ruta` retorna la ruta sugerida desde la sede del municipio hasta la ubicación del ticket.
- `GET /tickets/municipio/<id>/timeline` expone los cambios de estado y comentarios para armar una línea de tiempo del reclamo.

### Recomendaciones
1. Renderizar el mapa con **MapLibre GL JS** usando la clave de MapTiler disponible en `VITE_MAPTILER_KEY`.
2. Agregar un **heatmap** con la capa `puntos` proveniente del endpoint `/estadisticas/mapa_calor/datos`, utilizando el GeoJSON incluido en `heatmap_geojson` (o el formato indicado en `map_layers.heatmap.preferred_format`).
3. Usar **Chart.js** (o librería equivalente) para graficar la cantidad de tickets por categoría a partir de los datos recibidos.
4. Incluir campos de filtro (categoría, fechas, etc.) que se traduzcan en parámetros de consulta para el endpoint de datos.
5. En el perfil con mapa grande, reutilizar la misma lógica para mostrar la distribución de tickets o usuarios según corresponda.

### Nuevo dashboard unificado
- `GET /estadisticas/dashboard` ofrece en una sola respuesta los KPIs principales, los datos de mapas de calor, filtros aplicados y la estructura necesaria para construir tableros modernos tanto para municipios como para pymes.
- El payload incluye tarjetas (`cards`) listas para renderizar con librerías como **ECharts**, **Chart.js**, **ApexCharts** o **AntV**, y series temporales listas para conectar con componentes de línea/área.
- Para el modo PyME se complementa con métricas de ventas (ingresos, pedidos, clientes únicos, tasa de conversión) y colecciones de datos (`ventas_over_time`, `top_productos`, `ventas_por_region`) que permiten construir dashboards con visualizaciones como gráficos de área apilados, treemaps o mapas de calor.
- En municipios se reutiliza el mismo JSON enriquecido de `build_stats_for_municipio`, facilitando gráficos de barras apiladas, líneas de tendencia y diagramas de estado.
- Desde la UI se puede consumir el JSON y alimentar componentes modernos (por ejemplo, **MUI X Charts**, **Recharts** o **Kepler.gl** para mapas) aplicando filtros en vivo contra el backend mediante los parámetros `estado`, `categoria`, `fecha_inicio`, `fecha_fin`, `distrito` y `satisfactorio`.

Estas instrucciones permiten desarrollar toda la interfaz en el frontend sin requerir plantillas HTML dentro del backend.
