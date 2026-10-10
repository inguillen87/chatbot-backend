# SS-ORGANIZATION-SETUP: recorrido por tipo de organización

Base backend: 4b1223ff, preservando el guard de publicaciones coordinadas.
La proyección anterior usa cinco etapas gubernamentales incluso para empresas.
Se conserva implementation_journey v1 y se añade organization_setup opcional,
contrato tenant.implementation_journey.v2, en el payload de activación existente.
No hay endpoint, esquema, credenciales o proveedor nuevos.

## Política y alcance

El tipo normalizado proviene del tenant autenticado y de organization_workspace:
municipio, gobierno, colegio, empresa, pyme o una organización genérica. No se
selecciona el tipo en React ni se concede Full por una etiqueta de presentación.
Identidad, canales y equipo reutilizan sus verificaciones actuales. Municipios y
gobiernos conservan los controles territoriales y de participación. Colegios,
empresas y pymes agrupan contenidos, catálogo y cobros, sin exigir jurisdicción
gubernamental. Un tipo no reconocido recibe una orientación neutral.

Es una secuencia predeterminada por sector, no todavía un editor de selección de
módulos por cliente. Los bloqueos del plan se preservan; un paso completado no
certifica el plan Full, la salida productiva, el pago o la entrega de mensajes.
El avance se calcula con las evidencias del catálogo de activación existente;
este corte no amplía el alcance de esas verificaciones ni modifica sus estados.

Se incluyen sólo ID/slug y textos de orientación. El identificador privado de
WhatsApp no se devuelve. Su existencia genera una indicación de revisar la
conexión anterior; no es evidencia de que el canal esté conectado o entregando.

## Coherencia y seguridad

La proyección es de lectura. No aprovisiona usuarios, categorías, dominios, planes
o canales. Fuentes faltantes, duplicadas o con ready/locked contradictorios no se
marcan como comprobadas. Sólo se proponen enlaces internos al espacio correcto;
se rechazan destinos externos, de otra organización y acciones dirigidas a API.
Se respeta la autorización del handler original: la proyección no concede permisos.

Se añadieron 13 pruebas puras y dos escenarios al runner HTTP completo: contrato
municipal autorizado, rechazo entre tenants, proyección empresarial y coexistencia
con el v1. Las cuentas y la base de ese runner son desechables; autenticación,
middleware, servicios y handlers son reales. No opera con datos de clientes.
Los 75 casos focales (62 anteriores + 13 nuevos) corren en CI junto a las 12
pruebas HTTP completas, como evidencias separadas. Los resultados se anotan en el
PR de cada revisión una vez finalizados, no se presuponen por este documento.

Frontend coordinado: guía por pasos, acciones con scope/return_to, separación de
herramientas gubernamentales y descarte de respuestas atrasadas. Los fixtures de
contrato fueron generados con este builder, no escritos como supuestos del cliente.
La aceptación local de la SPA completa verifica navegación, scopes y tamaños.

## Publicación y pendientes

Publicar mediante un par frontend/backend con revisiones exactas y writer fence.
No mover dominios estables ni asumir aceptación institucional por un Preview READY.
Se conservan cuentas/WhatsApp de Junín y el MVP de Agente Conversa. Permanecen
pendientes la selección granular de módulos, dominios/branding white label, ocho
migraciones, escritura QA institucional y la operación real de proveedores.
