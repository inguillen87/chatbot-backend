# Corte institucional compatible con el backend productivo

Base exacta: `912446bf96f8330664a9dec009ae57dbf935c73c`.
Origen funcional: `e3bee31c60e1e3318a853ac445b3ee4b0c657540` (#2806).
Frontend existente: `01f9a79e7429c444467128bd964f8be7c0eed411` (#1800).

Este corte evita desplegar la rama completa de infraestructura para habilitar el agente institucional. Agrega sus tres módulos de contenido/servicio/rutas y la autorización de administración, conserva la integración exacta con responder_chatboc y retira la clave de conocimiento de la respuesta genérica de configuración.

Se porta el marcador optativo de autenticación sin escritura implícita: sólo las rutas marcadas omiten generar un entity token durante su lectura. No se cambia la validación de sesión, firma ni permisos de las demás rutas. El helper de mantenimiento es independiente de Flask, ORM y proveedores; no activa ni instala un sistema global de autoridad de escritura.

Quedan byte a byte en la base productiva: modelos, todas las migraciones, requirements, config, socket_service, gunicorn, apply_migrations y roles. El arranque con eventlet se conserva. El esquema esperado sigue en `20260820_survey_content_jurisdiction_v1`; NO se aplica la cadena de 16 migraciones, se repara ningún ticket, se cambia DATABASE_URL o se migra a Neon.

Se verificó en lectura el esquema real: TenantProfile, TenantConfig, AuditEvent, Role y UserRole ya contienen los campos requeridos. El respaldo solicitado en Render aparece con export del 29/09 y enlace de archivo; no se descargó ni se afirma haber ensayado su restauración.

## Validación local

Dependencias originales instaladas en un entorno virtual nuevo, fuera del worktree. Aprobaron 12 pruebas de contenido y 17 pruebas HTTP con la aplicación y base temporal reales, incluido el responder original, autorización entre organizaciones, relectura, versiones, publicación/retiro y fuentes.

Los mismos 17 casos también aprobaron con eventlet.monkey_patch() aplicado antes del arranque, sin cambiar el producto para acomodar la prueba. No se suman dos veces como casos distintos. El primer envoltorio de prueba tuvo un error de codificación Windows y otro no descubría casos; se corrigió el runner y se exige explícitamente la carga de 17 tests, no se usa un resultado de cero casos como aprobación.

No se certifica aquí Linux, tráfico real, interpretación de un LLM externo, login de Analía o WhatsApp nativo. No se tocaron cuentas nominales, credenciales protegidas, corpus privados ni datos de producción en los ensayos. Los archivos y logs del entorno local no se publican en el repositorio.

La escritura de un nuevo workflow fue bloqueada por la herramienta. No se reintentó por otra vía ni se considera CI certificado; se descartó el archivo incompleto y se ejecutó la validación local aislada. Este documento no marca un despliegue como realizado: el PR registra el resultado efectivo y el SHA que finalmente se publique.
