# Frontend Handoff — Analytics, Heatmaps, Realtime y Resumen IA

Este documento es para **ejecutar ya** en frontend y destrabar los problemas actuales:

- WebSocket cerrándose antes de conectar.
- Heatmap/analytics que cargan datos pero no se renderizan.
- Warnings de `width(-1) height(-1)` en charts.
- Necesidad de dashboard premium con resumen IA para encuestas.

---

## 1) Estado actual del backend (listo para consumir)

### Heatmap y estadísticas municipales
- `GET /api/estadisticas/mapa_calor/datos`
- `GET /api/estadisticas/tickets`

Ambos endpoints ya exponen estructura enriquecida para heatmap y metadata de render (incluye hints para provider). El frontend debe consumir estas rutas y **no depender de Google HeatmapLayer legacy**.

### Analytics de encuestas (con resumen ejecutivo)
- `GET /api/encuestas/<encuesta_id>/analytics/dashboard`
- `GET /api/encuestas/<encuesta_id>/analytics/brief`
- `GET /api/encuestas/<encuesta_id>/analytics/heatmap`
- `GET /api/encuestas/<encuesta_id>/analytics/forecast`
- `GET /api/encuestas/<encuesta_id>/analytics/alerts`

### Empleados (ya corregido)
- `POST /api/empleados`
- Alias legacy con POST habilitado:
  - `/api/municipal/empleados`
  - `/api/municipio/municipio/empleados`
  - `/api/municipal/municipio/empleados`

---

## 2) WebSocket — configuración frontend obligatoria

Error actual visto: `WebSocket is closed before the connection is established`.

### Reglas
1. Usar **misma base URL pública** del backend productivo (evitar mezclar dominios antiguos onrender y dominio canónico en la misma sesión).
2. Configurar explícitamente `path: "/api/socket.io"`.
3. Habilitar fallback (`transports: ["websocket", "polling"]`) para evitar caída total.
4. Si no hay token, enviar `auth: { channel: "web" }`.

### Snippet recomendado
```ts
import { io } from "socket.io-client";

const API_BASE = import.meta.env.VITE_API_BASE_URL; // ej: https://api.chatboc.ar

export const socket = io(API_BASE, {
  path: "/api/socket.io",
  transports: ["websocket", "polling"],
  withCredentials: true,
  auth: token
    ? { token, tenant_slug: tenantSlug }
    : { channel: "web", tenant_slug: tenantSlug },
  query: {
    tenant: tenantSlug,
    tenant_slug: tenantSlug,
  },
  reconnection: true,
  reconnectionAttempts: 10,
  reconnectionDelay: 800,
});
```

---

## 3) Heatmap premium: migrar a MapLibre (no Google HeatmapLayer)

La capa `google.maps.visualization.HeatmapLayer` ya está deprecada.

### Estrategia
- Motor principal: **MapLibre GL**.
- Fuente: `heatmap_geojson` o `heatmap` del backend.
- Fallback visual: scatter circles si no hay weight.

### Checklist de implementación
- [ ] Consumir `heatmap_geojson` si existe.
- [ ] Si no existe, transformar `heatmap[]` a GeoJSON FeatureCollection.
- [ ] Layer 1: `type: "heatmap"` con weight/intensity por `properties.weight`.
- [ ] Layer 2: `type: "circle"` para zoom alto (hotspots).
- [ ] Tooltip con categoría/estado/barrio.

---

## 4) Fix crítico de charts (`width(-1), height(-1)`)

Esto es **frontend layout**, no backend.

### Causa
El chart se monta antes de que el contenedor tenga tamaño real.

### Solución obligatoria
1. Contenedor con tamaño mínimo:
   - `min-width: 280px;`
   - `min-height: 220px;`
2. No renderizar chart hasta tener dimensiones válidas (`ResizeObserver`).

### Patrón recomendado
```tsx
const [size, setSize] = useState({ w: 0, h: 0 });

useEffect(() => {
  if (!ref.current) return;
  const ro = new ResizeObserver(([entry]) => {
    const { width, height } = entry.contentRect;
    setSize({ w: Math.floor(width), h: Math.floor(height) });
  });
  ro.observe(ref.current);
  return () => ro.disconnect();
}, []);

const canRender = size.w > 40 && size.h > 40;
```

---

## 5) Panel “increíble” (MVP 2 semanas)

## Semana 1
- [ ] Socket estable + fallback polling.
- [ ] Heatmap MapLibre con transición y hotspots.
- [ ] Cards KPI (`events_total`, `active_zones`, `top_hotspots`).
- [ ] Filtros persistentes por tenant y rango temporal.

## Semana 2
- [ ] Integrar `analytics/dashboard` de encuestas en una vista ejecutiva.
- [ ] Mostrar `brief` (resumen IA) arriba del dashboard.
- [ ] Alertas y forecast en panel lateral en tiempo real.
- [ ] Export CSV/PDF del bloque ejecutivo.

---

## 6) Contrato de errores (UX)

- Si heatmap viene vacío: mostrar estado `Sin eventos en el rango seleccionado`.
- Si socket cae: banner `Tiempo real desconectado` y reintento automático.
- Nunca renderizar JSON crudo en cards (normalizar strings/valores).

---

## 7) Eventos de observabilidad frontend (obligatorio)

Emitir eventos para medir estabilidad:
- `analytics_dashboard_loaded`
- `analytics_heatmap_rendered`
- `analytics_heatmap_empty`
- `analytics_socket_connected`
- `analytics_socket_disconnected`
- `analytics_widget_render_failed`

Campos mínimos: `tenant_slug`, `route`, `build_version`, `error_code`.

---

## 8) Definición de “listo para vender”

Se considera listo cuando:
- 99% de sesiones cargan dashboard sin errores de render.
- 0 warnings de `width(-1)` en producción.
- Heatmap visible en < 2s con datos reales.
- Resumen IA de encuesta visible en la cabecera de analytics.
- Realtime reconecta automáticamente sin intervención del usuario.

