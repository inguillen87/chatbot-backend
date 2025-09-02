# Directrices de mapas y estadísticas para el frontend

Para que el equipo de frontend implemente los mapas y las estadísticas solicitadas, utilizar los siguientes endpoints del backend:

- `GET /estadisticas/mapa_calor/datos` devuelve puntos geojson para construir un mapa de calor de tickets. Admite parámetros opcionales como `tipo_ticket`, `municipio_id`, `rubro_id`, `fecha_inicio`, `fecha_fin`, `categoria` y `estado`.
- `GET /estadisticas/usuarios/ubicaciones` entrega una lista de objetos `{lat, lng}` con las ubicaciones de los usuarios del mismo municipio.

### Recomendaciones
1. Renderizar el mapa con **MapLibre GL JS** usando la clave de MapTiler disponible en `VITE_MAPTILER_KEY`.
2. Agregar un **heatmap** con la capa `puntos` proveniente del endpoint `/estadisticas/mapa_calor/datos`.
3. Usar **Chart.js** (o librería equivalente) para graficar la cantidad de tickets por categoría a partir de los datos recibidos.
4. Incluir campos de filtro (categoría, fechas, etc.) que se traduzcan en parámetros de consulta para el endpoint de datos.
5. En el perfil con mapa grande, reutilizar la misma lógica para mostrar la distribución de tickets o usuarios según corresponda.

Estas instrucciones permiten desarrollar toda la interfaz en el frontend sin requerir plantillas HTML dentro del backend.
