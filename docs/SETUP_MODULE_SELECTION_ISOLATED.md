# Seleccion de preparacion por organizacion

Este corte vive en una rama aislada. El trabajo previo tenia dos implementaciones
mezcladas; se conservaron sus archivos y se separo esta version para revision.
No integrar ambas variantes. Debe acordarse un solo contrato antes del merge.

Permite guardar los objetivos de puesta en marcha: WhatsApp, catalogo, cobros,
encuestas y territorio, segun el sector. Cobros depende del catalogo.
No cambia la suscripcion, los permisos, los agentes ni conexiones operativas.
El progreso del asistente no equivale a aceptar la plataforma en produccion.

La seleccion usa revision esperada y auditoria atomica en el tenant existente.
El contrato publico excluye estas preferencias. No hay migraciones adicionales.
El plan y el permiso se revisan en servidor mediante los controles existentes.

Las pruebas focales y HTTP se registran en el PR. Queda pendiente reconciliar
el trabajo concurrente y completar la aceptacion visual antes de promover.
