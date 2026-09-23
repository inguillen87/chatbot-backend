# Guía privada por organización

Se reutiliza el archivo de 29 nodos de `f64a11aedfc076589e1924a16ecf31e6b03702f6`,
con los mismos bytes y SHA-256 `f028f657752ecc74c9cb1d7f4ae8408d210f42a9b0d8ccb6a9afc7bf83d4bf41`.
La fuente es el plan de IA accesible y Mesa Única de 14 páginas, con referencias
por nodo y validación institucional pendiente. El PDF original no se publica.

La configuración persistida del servidor habilita únicamente una guía conocida:
`TenantProfile.configuracion.private_conversation_guide = {"enabled": true,
"guide_id": "accessible-support-evaluation"}`. No se deduce del nombre, slug,
correo, plan, tipo de organización o parámetros del navegador. Los endpoints de
edición existentes no permiten guardar esta clave mediante su catálogo de campos.
Este corte no incluye un endpoint para activarla ni ejecuta una activación remota.

`GET /api/admin/tenants/<slug>/conversation-guide` acepta `node` (inicio por defecto)
y `selection` (código opcional). Responde `tenant.conversation_guide.v1`, scope del
tenant, fuente, huella, política de evaluación, nodo y textos de interfaz. Exige
sesión administrativa y autorización de control del tenant activo solicitado;
no sustituye un slug inexistente por el tenant de sesión ni aplica aliases.
Los parámetros adicionales de scope deben coincidir. Cache: `private, no-store`.

El descriptor `tenant.conversation_guide_access.v1` se publica como
`conversation_guide` en el bundle privado de configuración y dentro de
`channel_activation.organization_setup` en el perfil y la lectura de activación.
Se construye con el actor autorizado, sin leer la guía. Otros consumidores sin
actor reciben `null`. Abrir el recorrido carga el contenido; no altera el progreso
ni declara que la base de conocimiento esté lista.

La navegación ejecuta opciones explícitas de evaluación; no es comprensión libre
de lenguaje por IA. No utiliza los stubs de RAG, no carga el PDF en el catálogo de
descarga pública, no registra casos o encuestas y no llama proveedores. La fase
de autenticación del endpoint evita también la escritura implícita de entity token.
No agrega esquemas, presets, permisos, números o conexiones de WhatsApp.

Validación local: `python -m tests.private_conversation_guide_http --write-fixture`
usa la aplicación Flask real, login real y SQLite descartable, con red externa
bloqueada. Las 13 pruebas recorren los 29 nodos y sus enlaces, verifican denegaciones por identidad,
scope y configuración, evidencia de origen y ausencia de escrituras SQL. La fixture
compartida del frontend procede de estas respuestas. No certifica producción ni
la vigencia de requisitos institucionales. Además pasan 31 regresiones y 18 subtests
de activación, recorrido de preparación y perfil de organización.
