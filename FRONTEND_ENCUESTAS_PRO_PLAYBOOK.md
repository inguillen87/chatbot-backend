# Frontend Playbook PRO — Encuestas en Tiempo Real (Municipios + PyMEs)

Este documento es una guía de implementación **directa** para llevar la sección de encuestas a nivel consultora política / enterprise LATAM.

## 1) Objetivo UX/UI (modo vendible)

- Experiencia "broadcast": resultados en vivo con refresh suave (cada 3–5s), sin saltos visuales.
- Tablero ejecutivo + tablero operativo:
  - **Ejecutivo**: KPIs, tendencia, resumen IA, top hallazgos.
  - **Operativo**: evolución minuto a minuto, mapa de calor, filtros por canal/territorio.
- Modo presentación para pantallas grandes (war room / comando de campaña).

## 2) Endpoint clave para vivo

`GET /api/public/encuestas/{slug}/live-results`

### Payload recomendado (ya disponible backend)

- `total_respuestas`
- `preguntas[].opciones[].{value,votos,porcentaje}`
- `timeline_minute[]`
- `momentum.{last_10m,previous_10m,trend,delta}`
- `kpis.{responses_last_hour,participation_per_minute,heatmap_coverage_cells,leader}`
- `heatmap.{points,cells,metadata}`
- `ai_summary`
- `updated_at`



## 2.1 Query params para performance (nuevo)

En escenarios de alto tráfico o mobile, usar versión liviana:

`GET /api/public/encuestas/{slug}/live-results?include_heatmap=0&window_minutes=20&max_points=800&max_cells=120`

- `include_heatmap=0`: desactiva carga de puntos/celdas para vistas KPI-only.
- `window_minutes`: ventana de momentum (5..30) para ajustar sensibilidad.
- `max_points` / `max_cells`: controla payload para mantener TTFB bajo.

> Recomendación: dashboard ejecutivo usa `include_heatmap=0`; dashboard territorial usa heatmap activo.



## 2.2 Endpoints Enterprise para consultoras (nuevo)

Además de `live-results`, integrar estos endpoints en el panel admin:

- `GET /admin/encuestas/{id}/analytics/forecast?window_minutes=15&horizon_minutes=90`
  - Proyección de cierre con `projected_total`, tasa actual y confianza.
- `GET /admin/encuestas/{id}/analytics/alerts?window_minutes=15&min_activity=5`
  - Reglas tácticas para activar alertas operativas.
- `GET /admin/encuestas/{id}/analytics/brief`
  - Brief ejecutivo listo para directorio/cliente.

Recomendación UX:
- Mostrar forecast en card fija de “proyección de cierre”.
- Mostrar alerts en “centro de comando” con severidad (`high|medium|info`).
- Usar brief como base de reporte descargable/compartible.



## 2.3 Centro de comando territorial + anti-fraude (nuevo)

Nuevos endpoints recomendados para módulo avanzado:

- `GET /admin/encuestas/{id}/analytics/segments/compare?a_canal=web&b_canal=whatsapp`
  - Compara 2 segmentos (canal/género/edad/territorio) con distribuciones por pregunta.
- `GET /admin/encuestas/{id}/analytics/anomalies?burst_window_minutes=5&burst_threshold=10`
  - Señales anti-fraude / calidad de muestra (`risk_score`, IPs sospechosas, concentración geo, huellas repetidas).

Uso frontend sugerido:
- Card `SegmentComparator`: panel A vs B con barras espejo.
- Card `DataQuality`: semáforo de riesgo (`bajo|medio|alto`) y tabla de señales.


## 3) Arquitectura de frontend sugerida

## 3.1 Polling inteligente

- Poll base: 5s.
- Si `momentum.trend === "subiendo"`, bajar temporalmente a 3s.
- Si pestaña inactiva (`document.hidden`), subir a 15s.
- Si error > 2 consecutivos, backoff exponencial (5s → 10s → 20s) + banner no intrusivo.

## 3.2 Estado y cache

- Mantener `lastPayload` para transición animada (framer-motion / css transitions).
- No rehacer gráficos completos; actualizar series incrementalmente.
- Persistir 5 minutos en cache local para "volver" sin pantalla vacía.

## 3.3 Componentes UI (orden recomendado)

1. `LiveHeader`
   - Total respuestas, hora de actualización, chip de tendencia.
2. `ExecutiveKpis`
   - `responses_last_hour`, `participation_per_minute`, cobertura heatmap, líder actual.
3. `LiveMomentumChart`
   - `timeline_minute` en área/línea con eje relativo (últimos 60 min).
4. `QuestionRaceBoard`
   - Barras por opción con `%` y microanimación de cambio.
5. `GeoHeatmapPanel`
   - Toggle puntos/celdas.
   - Tooltip: barrio/canal/recuento.
6. `AiSummaryCard`
   - Texto IA + botón "copiar para reporte".



## 3.4 Stack tecnológico recomendado (SaaS global)

- Gráficos: **ECharts** (alto rendimiento) o **Recharts** (rápido de iterar).
- Mapas/heatmap: **MapLibre GL** + layer heatmap y clusters.
- Estado servidor: **TanStack Query** con refetch inteligente.
- Estado UI: Zustand/Redux Toolkit solo para filtros persistentes.
- Animaciones: Framer Motion (transiciones suaves sin jitter).


## 4) Modo campaña / consultora

- Escena "Presentación":
  - tipografía grande,
  - auto-ciclo entre preguntas cada 10s,
  - full screen.
- Export rápido para cliente:
  - PNG del tablero,
  - CSV de respuestas,
  - texto de resumen IA para WhatsApp/Email.

## 5) UX de filtros (clave comercial)

- Filtros persistentes por sesión: canal, barrio/ciudad/provincia.
- Quick presets:
  - "Última hora"
  - "Hoy"
  - "Últimas 24h"
- Botón "Reiniciar filtros" visible siempre.

## 6) Diseño visual recomendado

- Tema oscuro profesional para war-room.
- Colores:
  - `subiendo` = verde,
  - `estable` = azul,
  - `bajando` = ámbar.
- Skeleton loaders en cada panel (evitar blank states).
- `updated_at` visible en todos los módulos críticos.

## 7) Checklist de calidad para salir a demo internacional

- [ ] Los números del header y de gráficos no "saltan" abruptamente.
- [ ] Heatmap responde en < 300ms al filtrar.
- [ ] Cambios de tendencia se reflejan visualmente (chip + flecha + color).
- [ ] El resumen IA está siempre presente (aunque sea fallback neutral).
- [ ] En mobile, tarjetas colapsables y prioridad a KPI + pregunta líder.

## 8) Copy sugerido (comercial)

- "Monitoreo electoral en tiempo real con analítica territorial y narrativa automática."
- "Sondeos, encuestas y participación ciudadana en una sola vista ejecutiva."
- "Inteligencia accionable para municipios, campañas y equipos corporativos."

---

Si querés, en el próximo paso te armo:

1. **wireframe completo** (desktop + mobile),
2. **especificación de componentes React** (`props`, `loading`, `error`, `empty`),
3. **guía de diseño visual premium** (tokens de color, tipografía, espaciado),
4. **script de demo comercial de 7 minutos** para consultoras políticas.
