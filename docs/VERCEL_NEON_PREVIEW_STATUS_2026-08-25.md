# Chatboc Vercel + Neon — estado verificable de Preview

Fecha de corte: 2026-08-25
Alcance: entorno Preview y rama Neon QA. Producción y Render quedan fuera de este corte.

## Resultado ejecutivo

El frontend y el backend están desplegados en Vercel Preview, integrados entre sí y validados contra una copia QA en Neon. La demo de gobierno incluye tablero ejecutivo, encuesta, reclamos/casos, mapa territorial, analítica, QR/WhatsApp y tiempo real. Los datos sintéticos están identificados como demostración y no se presentan como verdad municipal.

No se autorizó todavía el corte de Producción ni la baja de Render. Esos pasos requieren completar los gates externos y una verificación con escritura controlada.

## Evidencia por fase

| Fase | Estado | Evidencia |
| --- | --- | --- |
| Inventario y aislamiento | Completa | Trabajo realizado en ramas/worktrees aislados; Producción y Render sin cambios. |
| Neon QA | Completa | Rama QA validada con 170 tablas y 52.751 filas; rama principal sin mutaciones. |
| Backend Vercel Preview | Completa | Deployment `dpl_EKUZe4NHX5WvjZRLiv9iqcEhbTrc`, región `gru1`, estado `Ready`, runtime `0a5fd6faa`. |
| Frontend Vercel Preview | Completa | Deployment `dpl_FyqvHD4DNppT3WkboA2LxCH33KWv`, estado `Ready`, commit `80d3ada8`. |
| Demo ejecutiva y territorial | Completa en Preview | Contrato `demo.admin_preview.v1`, mapa MapLibre, KPIs reconciliados y responsive. |
| Aceptación remota read-only | Completa | 3 pruebas pasaron y 1 prueba durable quedó omitida por `WRITE_QA=0`; sin escrituras. |
| Arranque en frío | Mitigado, no resuelto estructuralmente | Primer request tras scale-to-zero: aproximadamente 16 s. Requests activos: aproximadamente 0,2–0,4 s. |
| Turnstile real | Pendiente externo | Requiere completar la autorización de Cloudflare y probar el desafío humano real. |
| Producción y retiro de Render | Pendiente | No ejecutar hasta completar backup, migración final, smoke con escritura, webhooks y rollback. |

## URLs correctas de Preview

- Demo ejecutiva: `https://chatboc-r2-preview.vercel.app/demo?sector=gobierno&rubro=municipio&tenant_slug=junin&remote_preview_qa=1`
- Encuesta territorial Junín: `https://chatboc-r2-preview.vercel.app/e/demo-gobierno-junin-prioridades-barriales?tenant_slug=junin`
- Backend de Preview: `https://api-preview.chatboc.ar`
- Health de Preview: `https://api-preview.chatboc.ar/api/health`

No usar para la presentación la variante genérica con `tenant_slug=municipio`: corresponde a un escenario sintético genérico y no al escenario territorial de Junín.

## Precalentamiento para una reunión

Vercel Preview reduce a cero el contenedor luego de un período corto sin actividad. Para que el primer click de la reunión no pague el cold start, ejecutar desde el repositorio backend:

```powershell
.\scripts\warm_vercel_preview.ps1 -DurationMinutes 120
```

El script:

- solo permite HTTPS hacia `api-preview.chatboc.ar`;
- solo ejecuta `GET /api/health`;
- rechaza el dominio de Producción, otras rutas, queries y fragmentos;
- corre durante un plazo acotado y reporta éxitos, fallos y latencia;
- no escribe datos ni contiene secretos.

Iniciarlo entre dos y cinco minutos antes de la presentación y mantener esa terminal abierta durante la reunión.

## Gates antes de apagar Render

1. Crear backup verificable de la base origen y registrar conteos/constraints críticos.
2. Ejecutar la migración final hacia la rama/base Neon destinada a Producción.
3. Migrar variables de entorno sin imprimir secretos y comprobar que no quedan referencias a Render.
4. Desplegar un SHA exacto de backend y frontend en Vercel Production.
5. Validar health, autenticación, tenant isolation, encuesta, reclamo, analítica, subida de archivos y tiempo real.
6. Ejecutar una participación/voto QA durable y reconciliarla en UI, API y base.
7. Validar webhooks reales de WhatsApp/Twilio y Turnstile con evidencia externa.
8. Confirmar rollback a Render o al deployment previo durante la ventana de corte.
9. Recién después, detener y eliminar los servicios pagos de Render.

Un deployment `Ready` prueba que Vercel construyó y arrancó el artefacto; no reemplaza la evidencia completa UI → API → DB → proveedor.
