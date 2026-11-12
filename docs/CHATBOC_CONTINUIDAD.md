# Chatboc · Conversa TDF · Junín — continuidad compartida

Índice vigente desde el 12 de noviembre de 2026.

## Tarea única

**[Chatboc oficial: funcionalidades, UX y diseño — #2827](https://github.com/inguillen87/chatbot-backend/issues/2827)**

Esta tarea reúne prioridades, responsables, PR y evidencia. Actualizar allí el
estado de ejecución; no copiar el backlog completo en cada repositorio ni abrir
otra tarea general. Los issues especializados y PR anteriores se conservan como
entregas vinculadas.

## Base recuperada

| Repositorio | Rama de continuidad | Revisión observada |
| --- | --- | --- |
| Backend, PR #2825 | `fix/canonical-contact-export-20261103` | `f797d6a114d4dcf4bc6e5008f7b0e5283183d44a` |
| Frontend, PR #1817 | `fix/tracking-location-provenance-20261103` | `89302ec29424c9eed0ec76c83a3e42d327a82a25` |

Estas revisiones son bases de trabajo observadas, no certificaciones de
producción. Los PR están apilados y sus descripciones contienen resultados de
revisiones anteriores. Obtener el head/base actuales antes de integrar.
Los ZIP del proyecto y `main` son anteriores a esta pila de trabajo.

## Coordinación

- ChatGPT/Codex lleva el primer corte de persistencia de preferencias y movimiento accesible.
- Claude fue localizado por su [PR frontend #1819](https://github.com/inguillen87/chatboc-frontend/pull/1819). Se dejó el [mensaje de coordinación](https://github.com/inguillen87/chatboc-frontend/pull/1819#issuecomment-6827760959); su acuse sigue pendiente.
- Cada agente registra objetivo, rama/base/head, archivos a cargo y criterio de cierre antes de editar. Un escritor por rama.
- Conservar contratos, autoridad de sesión, tenant y dominios institucionales. Reconciliar trabajo ajeno sin force-push ni reemplazarlo por snapshots.
- Las nuevas ramas de este corte tienen autodeploy desactivado. Registrar pruebas locales, CI, preview y publicación por separado.

La otra línea de trabajo del mismo día se identificó en frontend #1818
(continuidad de escritura y mapa) y backend #2826 (inicialización territorial).
Se dejó un mensaje en #1818 y sus cambios se componen en los PR #1820 y #2828,
conservando intactas las ramas de origen. El aporte global de
Claude aplica la política inicial de movimiento del sistema a Framer Motion;
Confetti cubre por separado canvas y cambios de preferencia en vivo.

La unificación alcanza decisiones, código y referencias accesibles. No fusiona
físicamente historiales de ChatGPT, no transfiere sesiones y no recupera cambios
locales sin commit. El enlace de la otra charla recién creada sigue pendiente.

## Referencias conservadas

- [Registro anterior de continuidad](CONTINUITY_CHAT_WORK_CODEX_20260922.md).
- [Experiencia frontend compartida](https://github.com/inguillen87/chatboc-frontend/blob/89302ec29424c9eed0ec76c83a3e42d327a82a25/docs/FRONTEND_SINGLE_TASK_WORLD_CLASS_CHAT.md).
- [Acceso institucional TDF](ORGANIZATION_ACCESS_TDF_SPRINT.md).
- [Contrato del piloto TDF](tdf_meta_sandbox.contract.md).
- [Conocimiento institucional, #2805](https://github.com/inguillen87/chatbot-backend/issues/2805).
- [Reconciliación de infraestructura, #2808](https://github.com/inguillen87/chatbot-backend/issues/2808).
- [Efectos durables, #2775](https://github.com/inguillen87/chatbot-backend/issues/2775).

Las fechas, bloqueos y resultados de esos documentos son históricos. La tarea
única registra qué se volvió a comprobar y qué queda pendiente.
