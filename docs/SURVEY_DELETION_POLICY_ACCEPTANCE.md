# Protección del borrado administrativo de encuestas

## Pareja aceptada y alcance

Estado verificado el 22 de septiembre de 2026: aceptación integrada aislada
**aprobada**; publicación coordinada y comprobación del entorno desplegado
**pendientes**. Esta actualización documental corrige el P2 de Codex
`discussion_r4067871705` del PR backend #2794. No cambia código ejecutable.

| Referencia | Revisión exacta |
| --- | --- |
| Base backend del PR #2794 | `f13cd73f6161fdd003edbc86bc6fc2506235e484` |
| Head backend cuya implementación fue aceptada | `a6b7dd44de3862e4936008e0f0c146333fcb460b` |
| Checkout backend ejecutado por la CI del PR | `d1a881d08a48310404577d654494f5684f29c456` |
| Frontend coordinado, PR #1761 | `1e8b8104c79bc8a1391d0076c25167329f8f62d2` |

El checkout backend es la unión de prueba del head con su base; no es un merge
productivo. La revisión frontend de esta tabla es la fijada tanto en
`.github/workflows/survey-deletion-policy.yml` como en
`tests/run_survey_workspace_browser.py` y en el reporte `persistence.json`.
La revisión frontend anterior `ec63a2e...` no es la pareja aceptada final.
No se cambia la línea de selección de módulos ni se introducen clientes nuevos.

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

El guard no borra ni confirma la transacción. La consulta de existencia de respuestas
lee un ID con límite, no carga contenido ni cuenta toda la tabla. Los mensajes y
códigos del rechazo proceden del backend, con instrucciones para releer/revisar.

Alcance exacto: entradas HTTP administrativas compartidas. No se reescribe el
servicio interno `delete_encuesta` ni se afirma que un caller directo esté
cubierto. No existe excepción silenciosa para borrar respuestas de prueba.

## Evidencia final ejecutada

Backend: [run 35676839086](https://github.com/inguillen87/chatbot-backend/actions/runs/35676839086),
job `106585025048`, finalizado correctamente. Los pasos finales se volvieron a
consultar al retomar desde el chat; no se lanzó una nueva CI para esta corrección
documental ni se presenta la evidencia previa como una ejecución nueva.

| Suite | Resultado y alcance |
| --- | --- |
| Política de borrado | 17 pruebas aprobadas: guard real, SQLite/SQLAlchemy y adaptadores sintéticos de permisos/lock |
| HTTP de aplicación completa | 12 pruebas aprobadas: `create_app`, login/cookies/modelos/middleware originales y cuentas/SQLite desechables |
| Navegador del panel SPA completo | 4 recorridos aprobados con el frontend exacto de la tabla, backend original y sin respuestas de API simuladas |

Los recorridos integrados cubren escritorio de 1440 px (cierre), tablet de 820 px
(borrado), móvil oscuro de 390 px (cierre con recuperación de lectura) y 320 px
(borrado con recuperación de lectura). Incluyen ingreso por el formulario real,
búsqueda, identidad visible del instrumento en la confirmación, teclado,
mutación, lectura posterior, recarga de página y sesión conservada.

En los dos escenarios de recuperación, el runner provoca un 503 sólo en las
lecturas posteriores al commit. El panel muestra actualización pendiente,
bloquea acciones y recupera mediante lectura sin repetir la mutación de encuesta.
Se observa exactamente una mutación de encuesta por escenario. Login, preferencias
auxiliares de la SPA y controles del runner son solicitudes distintas.

`persistence.json` comprueba en los modelos originales que las dos encuestas
cerradas siguen presentes con una respuesta cada una, y que los dos borradores
eliminados ya no existen. Los controles de fallos sólo existen en el runner.

Artefacto integrado: `survey-workspace-acceptance`, ID `10672917734`,
2.959.367 bytes, 12 archivos. SHA-256 del ZIP comprobado nuevamente:
`ee31b110c22a9a267199586d4bf6fc2288d9b647760cbc10bae72e518114b4b5`.

Frontend: [run 35676754880](https://github.com/inguillen87/chatboc-frontend/actions/runs/35676754880),
job `106584776041`, tipos, build y pruebas aprobados. El reporte Vitest del
artefacto `10673446661` confirma 3.313 pruebas aprobadas en 423 archivos,
cero fallidas y cero pendientes. SHA-256 del ZIP comprobado nuevamente:
`1ea5fdb433d0f24383c8d86ebeb503ea7fb2476221fb3e4329e86d35c4e6791f`.
Sus cuatro recorridos de tarjeta con callbacks sintéticos son evidencia separada
de los cuatro recorridos integrados; no se suman como una sola certificación.
Los artefactos de CI tienen retención limitada: comprobar disponibilidad antes
de usarlos como única copia de evidencia.

## Reproducción aislada

Usar checkouts limpios: backend `d1a881d08a48310404577d654494f5684f29c456`
y frontend `1e8b8104c79bc8a1391d0076c25167329f8f62d2`. Reproducir los pasos
del workflow, con Python 3.12.14, Node 24 y las dependencias fijadas en los
repositorios. No cargar archivos `.env` ni bases o claves de clientes.
El runner comprueba el SHA frontend y crea identidades y SQLite desechables;
no reemplazar esa comprobación para ejecutar otra revisión y atribuirle este
resultado. Una nueva pareja requiere evidencia propia.

## Fallos intermedios corregidos

La primera ejecución buscaba `admin_lifecycle` en el detalle legacy; la aserción
se corrigió para leer el contrato del listado, conservando la comprobación del
estado en el detalle. Después se corrigió el harness para distinguir la navegación
HTML `/admin` del proxy de API legacy. No se falsearon payloads administrativos.
El recorrido real detectó luego que faltaba el nombre del instrumento en el
diálogo. Frontend `1e8b8104` lo muestra en ambas confirmaciones, escapado y sin
truncar, con cuatro regresiones nuevas. Los intentos fallidos no se cuentan como
aceptaciones; el resultado válido es el run final identificado arriba.

## Publicación y límites

La aceptación del navegador integrado aislado ya está completada. Sigue pendiente
la publicación coordinada y su verificación autenticada en el entorno desplegado.
No se han promovido estos PR a producción. No hay cambios de esquema/dependencias,
escrituras sobre encuestas de clientes, números de WhatsApp, planes o callbacks.

La evidencia aislada no certifica concurrencia PostgreSQL, dispositivos físicos,
PWA instalada, MFA ni el funcionamiento de Meta/Twilio. Los planes de las cuentas
de prueba sólo se modificaron en SQLite desechable. No se debe reutilizar esta
evidencia para afirmar que el dominio público ya sirve el candidato.

Continuidad del trabajo entre interfaces: `CONTINUITY_CHAT_WORK_CODEX_20260922.md`.
