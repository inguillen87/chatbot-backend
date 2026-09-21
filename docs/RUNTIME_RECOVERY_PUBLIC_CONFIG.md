# Configuración pública de recuperación

Base: revisión productiva 912446bf96f8330664a9dec009ae57dbf935c73c.
No se incorpora la cadena de migración ni la selección/branding #2790.

GET /api/config/runtime-recovery publica chatboc.runtime_recovery_ui.v1 desde
config/runtime_recovery_ui.json, fuente mantenida por el backend. Incluye textos
para seis estados técnicos y las etiquetas accesibles de región/acciones. Su scope
es platform: no contiene ni infiere configuración privada de una organización.
No consulta bases de datos, crea sesiones o llama proveedores. No acepta métodos
de escritura. No cambia el contrato de /api/version, /api/config o maps.

El consumidor valida esquema y límites, muestra texto escapado y no usa literales
de respaldo cuando no recibió configuración válida. Puede conservar esta copia
pública en memoria durante el documento; no necesita tokens ni almacenamiento
persistente. Una primera visita completamente offline, o un backend anterior sin
este endpoint, mantiene la aplicación montada pero no inventa la nueva barra.
El contenido es configuración de plataforma, no prueba de conectividad, login,
entrega de mensajes, guardado de negocio ni autorización.

Pruebas: diez casos sobre el blueprint real registrado en una aplicación Flask
desechable. Incluyen métodos rechazados, privacidad, configuración exacta,
compatibilidad de endpoints previos y fallos de archivo. No equivalen a ejecutar
create_app completo ni a una sesión institucional/proveedor en producción.
La CI instala Flask con el pin ya declarado en requirements.txt; no se modifica
ninguna dependencia del proyecto. Resultados se registran en el PR al terminar.

Coordinado con frontend #1758. Debe publicarse y comprobarse el backend antes de
presentar la nueva barra como disponible. No se ha desplegado este corte. Quedan
intactos writer fences, migraciones, bases, planes, números y callbacks.
