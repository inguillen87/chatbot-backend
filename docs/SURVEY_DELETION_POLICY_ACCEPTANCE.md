# Protección del borrado administrativo de encuestas

## Pareja aceptada y alcance

Aceptación integrada aislada **aprobada**; publicación coordinada y comprobación
del entorno desplegado **pendientes**. Este registro corrige el P2 de Codex
`discussion_r4067871705` del PR backend #2794. Sus revisiones y resultados son
históricos e inmutables: no representan el resultado de commits posteriores.

| Referencia | Revisión exacta |
| --- | --- |
| Base backend del PR #2794 | `f13cd73f6161fdd003edbc86bc6fc2506235e484` |
| Head backend cuya implementación fue aceptada | `a6b7dd44de3862e4936008e0f0c146333fcb460b` |
| Checkout backend ejecutado por la CI del PR | `d1a881d08a48310404577d654494f5684f29c456` |
| Frontend coordinado, PR #1761 | `1e8b8104c79bc8a1391d0076c25167329f8f62d2` |

El checkout backend es la unión de prueba del head con su base, no un merge
productivo. La revisión frontend es la fijada en
`.github/workflows/survey-deletion-policy.yml`,
`tests/run_survey_workspace_browser.py` y `persistence.json`.
La revisión anterior `ec63a2e...` no es la pareja aceptada final.
No se cambia la línea de selección de módulos ni se introducen clientes nuevos.

## Protección del producto

El servicio de borrado existente verifica tenant, gobernanza, materialización y
recibos institucionales. Este corte añade una precondición HTTP explícita de
borrador sin respuestas. Ocultar el botón no basta frente a solicitudes directas
o una pantalla desactualizada.

El guard en la fábrica compartida de siete rutas administrativas:
1. Verifica acceso antes de bloquear o consultar respuestas.
2. Usa el lock existente y vuelve a leer estado y organización.
3. Rechaza con 409 cualquier estado distinto de borrador.
4. Rechaza con 409 cualquier respuesta registrada, incluso sintética o inconsistente.
5. Mantiene las comprobaciones previas del servicio de eliminación.

El guard no borra ni confirma la transacción. La consulta de existencia lee un ID
con límite, no carga contenido ni cuenta toda la tabla. Los mensajes y códigos
del rechazo proceden del backend, con instrucciones para releer/revisar.
Alcance exacto: entradas HTTP administrativas compartidas. No se reescribe
`delete_encuesta` ni se afirma cubierto un caller interno que omita el guard.
No existe excepción silenciosa para borrar respuestas de prueba.

## Evidencia de la pareja aceptada

Backend: [run 35676839086](https://github.com/inguillen87/chatbot-backend/actions/runs/35676839086),
job `106585025048`, finalizado correctamente.

| Suite | Resultado y alcance |
| --- | --- |
| Política de borrado | 17 aprobadas: guard real, SQLite/SQLAlchemy, adaptadores sintéticos de permisos/lock |
| HTTP de aplicación completa | 12 aprobadas: `create_app`, login/cookies/modelos/middleware originales y cuentas/SQLite desechables |
| Navegador del panel SPA completo | 4 recorridos aprobados con el frontend exacto de la tabla, backend original y sin respuestas de API simuladas |

Los recorridos cubren 1440 px (cierre), 820 px (borrado), 390 px oscuro (cierre con
recuperación) y 320 px (borrado con recuperación). Incluyen formulario real de
ingreso, búsqueda, identidad del instrumento, teclado, mutación, lectura
posterior, recarga de página y sesión conservada.

En los dos casos de recuperación, el runner provoca 503 sólo en las lecturas
posteriores al commit. El panel muestra actualización pendiente, bloquea acciones
y recupera mediante lectura, sin repetir la mutación de encuesta. Se observa
una mutación de encuesta por escenario; login, preferencias auxiliares y
controles del runner son solicitudes distintas.

`persistence.json` comprueba en los modelos originales que las dos encuestas
cerradas siguen presentes con una respuesta cada una, y que los dos borradores
eliminados ya no existen. Los controles de fallos sólo existen en el runner.

Artefacto integrado: `survey-workspace-acceptance`, ID `10672917734`,
2.959.367 bytes, 12 archivos. SHA-256 del ZIP:
`ee31b110c22a9a267199586d4bf6fc2288d9b647760cbc10bae72e518114b4b5`.

Frontend: [run 35676754880](https://github.com/inguillen87/chatboc-frontend/actions/runs/35676754880),
job `106584776041`, tipos, build y pruebas aprobados. Vitest del artefacto
`10673446661`: 3.313 pruebas en 423 archivos, cero fallidas y cero pendientes.
SHA-256 del ZIP:
`1ea5fdb433d0f24383c8d86ebeb503ea7fb2476221fb3e4329e86d35c4e6791f`.
Los cuatro recorridos de tarjeta con callbacks sintéticos son evidencia separada
de los cuatro integrados; no se suman como una sola certificación.
Los artefactos tienen retención limitada: comprobar disponibilidad antes de
usarlos como única copia de evidencia.

## Reconsulta y nuevas ejecuciones

Al retomar el 22/09/2026 se consultaron los pasos de ambos jobs anteriores,
se comprobaron otra vez los digests de los ZIP conservados y se leyeron sus
reportes. Esa reconsulta no es una nueva ejecución ni una nueva certificación.

El push documental `a6d5bcd77cf857a39240e507a034990ad8d18b76` sí activó
**automáticamente** el workflow `35781755684`. No se inició manualmente ni se
atribuyó a ese run un resultado mientras seguía en curso. También los commits
posteriores de documentación pueden activar CI: consultar su estado por SHA.
La afirmación inicial de que no habría otra CI era demasiado amplia; se corrige
expresamente aquí sin alterar ni reemplazar la evidencia histórica de la tabla.

## Reproducción aislada

Usar checkouts limpios: backend `d1a881d08a48310404577d654494f5684f29c456`
y frontend `1e8b8104c79bc8a1391d0076c25167329f8f62d2`. Reproducir los pasos
del workflow con Python 3.12.14, Node 24 y las dependencias de los repositorios.
No cargar `.env`, bases ni claves de clientes. El runner comprueba el SHA
frontend y crea identidades/SQLite desechables. No reemplazar esa comprobación
para atribuir este resultado a otra revisión: una nueva pareja requiere evidencia propia.

## Fallos intermedios corregidos

La primera ejecución buscaba `admin_lifecycle` en el detalle legacy; se corrigió
la aserción al contrato del listado, conservando la lectura del estado del detalle.
Después el harness distinguió navegación HTML `/admin` de la API legacy sin
falsear sus payloads. El recorrido real detectó que faltaba el nombre en el
diálogo; frontend `1e8b8104` lo muestra en ambas confirmaciones, escapado y sin
truncar, con cuatro regresiones nuevas. Los intentos fallidos no se cuentan como
aceptaciones; el resultado válido es el run identificado en la tabla.

## Publicación y límites

La aceptación del navegador integrado aislado está completada. Sigue pendiente
la publicación coordinada y la verificación autenticada en el entorno desplegado.
No se han promovido estos PR a producción. No hay cambios de esquema/dependencias,
escrituras sobre encuestas de clientes, números de WhatsApp, planes o callbacks.

La evidencia aislada no certifica concurrencia PostgreSQL, dispositivos físicos,
PWA instalada, MFA ni Meta/Twilio. Los planes de las cuentas de prueba sólo se
modificaron en SQLite desechable. No usar esta evidencia para afirmar que el
dominio público sirve el candidato.
Continuidad: `CONTINUITY_CHAT_WORK_CODEX_20260922.md`.
