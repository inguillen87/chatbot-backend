# Frontend Next Pack (Widget + CRM)

## 1) Widget: respuesta comercial estructurada

Cuando `fuente` sea `catalogo_qdrant_con_promos_v2`, `catalogo_fallback_faq` o `catalogo_fallback_web`:
- renderizar bloque **Resumen**,
- renderizar hasta **3 productos destacados**,
- mostrar CTA fijos:
  - `pedir_presupuesto_pyme`
  - `hablar_con_agente_pyme_catalogo`
  - `ver_catalogo_pyme_buscar_otra`

## 2) Widget: captura de lead (1/3, 2/3, 3/3)

Sobre `pedir_info`, mostrar barra de progreso:
- `nombre` -> 1/3
- `telefono` -> 2/3
- `email` -> 3/3

## 3) Superadmin: cola de calidad de catálogo

Nuevo endpoint backend:
`GET /api/admin/catalog/quality?tenant_slug=<slug>&limit=100`

Render esperado:
- tabla con `confidence_score`, `quality_issues`, `review_required`,
- filtros por tenant,
- badge rojo para `review_required=true`.

## 4) Superadmin: SLA y lead score

`GET /api/admin/leads/interactions` ahora devuelve:
- `sla_breached` (true/false)
- `lead_score` (número)

Render recomendado:
- badge SLA cuando `sla_breached=true`,
- ordenar por `lead_score` por defecto.
