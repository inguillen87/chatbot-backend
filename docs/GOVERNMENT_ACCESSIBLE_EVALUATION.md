# Preparación de evaluación: atención gubernamental accesible

## Estado y alcance de este corte

Implementado en código, no aplicado a un tenant remoto: nuevo preset reutilizable
`government-disability-support` v1.0.0 en el registro y contrato existentes de blueprints.
No crea otra aplicación por cliente. No cambia las reglas de Full, permisos o MFA.
La solicitud actual permite adelantar preparación de evaluación antes del primer pago;
no demuestra pago, contratación ni habilitación productiva. No se creó una cuenta ni
se envió invitación: falta confirmar el correo real de la persona evaluadora.

## Fuente revisada

Archivo original local: `Plan de Implementacion IA Accesible y Mesa Única para Personas de Discapacidad.pdf`.
14 páginas; SHA-256 `1f6de63d4f70ede4e967077cc9eae768c64574b022d66c5ebb8868aac3d4a6ee`.
Texto completo leído y tablas de páginas 7–8 revisadas visualmente; página 6 en blanco.
El documento no se incorpora al repositorio público. El preset es genérico y no incluye
identidades, números, URLs, personas, documentación sensible ni credenciales del cliente.

| Requisito del documento | Páginas | Preparación / estado real |
| --- | --- | --- |
| Cinco áreas: documentación, salud, pensión/licencias, escuela/apoyos y trabajo | 4–5, 7–12 | Categorías del nuevo preset |
| Persona destinataria y familiar/red de apoyo | 1, 8–9 | Descripción de CRM; implementación de representación/consentimiento por validar |
| WhatsApp oficial y widget | 2 | Declarados como canales deseados; ambos desactivados al aplicar la base |
| Derivación humana con nombre, ciudad, contacto, motivo y registro | 12–13 | Modelo supervisado y auditoría requeridos; flujo real no certificado |
| CUD/CMO/RUPE y listas de cotejo | 1, 7–12 | Eje de contenidos; requisitos y fuentes deben aprobarse antes de publicar |
| Alertas CUD con 90 días de anticipación y otras notificaciones | 1, 3, 9–12 | Pendiente: fechas autorizadas, reglas, consentimiento, proveedor e idempotencia |
| Localidades Ushuaia, Río Grande y Tolhuin | 3, 10, 12 | Configuración específica futura del tenant; sin coordenadas o jurisdicción inventadas |
| Dos preguntas de cierre: resolución y facilidad 1–5 | 13 | Separadas en el alcance; cuestionario y disparo del cierre aún por configurar |
| Métricas de volumen, cierre, derivación, cotejo y tiempos | 2–3 | Deben derivar de eventos reales; no se insertan valores de demostración |
| Validación institucional y piloto de usabilidad con personas destinatarias | 3–4 | Gate de aceptación, no sustituido por unit tests |

## Decisiones que no se inventan

La columna «Fuentes de acceso» está vacía en las tablas del original. No hay evidencia
suficiente para declarar acceso a CUD, RUPE, OSEF, turnos, coberturas o farmacia.
El documento solicita DNI al inicio; eso no acredita identidad ni representación.
Propuesta pendiente de aprobación: orientación general sin DNI y verificación adicional
sólo al consultar datos personales o ejecutar un trámite. En evaluación no se piden
DNI, documentos médicos ni datos reales. Esta propuesta no se presenta como requisito
ya aprobado ni modifica automáticamente los circuitos existentes.
Las condiciones de prestaciones descritas en el original son contenido a validar,
no reglas legales/médicas certificadas por este desarrollo.

## Semántica del preset

El cargador existente valida el manifiesto y su digest. La previsualización no escribe;
la aplicación incorpora sólo valores faltantes en el namespace
`configuracion.government_disability_support`, preserva los previos y genera el recibo
existente. No materializa por sí sola categorías operativas, cuestionarios, usuarios,
conexiones o permisos. No afirmar «entorno configurado» por guardar el namespace.
La base `government-core` permanece intacta y como primera opción por compatibilidad.
La activación de la mesa real sigue su circuito independiente; el nuevo preset no la
activa ni reemplaza las comprobaciones de la base gubernamental general.

## Acceso de evaluación y próximos gates

Preparar invitación nominal al correo confirmado, con credencial propia o acceso de
un solo uso, expiración/revocación y alcance exclusivo de su organización. No usar
correo inventado, contraseña compartida, verificación de email ficticia ni tokens
firmados manualmente para eludir la sesión administrativa y su segundo factor.
Evaluación con datos sintéticos, sin envíos/cobros; Full productivo se confirma por
el circuito comercial. Usuarios, proveedores, dominio y esquema se validan antes de
activar el espacio remoto. El MVP y su alias actual se conservan.

## Evidencia de pruebas

`tests/test_government_support_preset.py`: 12 pruebas focales del cargador, validador,
catálogo y proyección reales. Los límites de importación ORM se sustituyen por objetos
que fallan ante acceso a DB. No son pruebas de la aplicación completa, RBAC, persistencia
remota o atención real. Deben complementarse con el circuito autenticado antes de invitar.
