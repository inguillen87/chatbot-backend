# Error de ingreso Clerk en Preview: DISTINCT sobre columnas JSON

Base: `3e735b3d6548790734aa4944e899bf28760776f6`.
La captura del usuario mostró POST /api/auth/clerk/session 500 y GET /api/rubros/ 503. La ruta efectiva de la SPA de Preview usa el backend inmutable `chatboc-backend-j5khkmhic-marcelos-projects-c26aa499.vercel.app`, no el alias antiguo api-preview. Los logs de ese backend confirmaron Session sync failed error_type=InternalError.

Se reprodujo la proyección de activación del tenant con el rol limitado del candidato en una transacción PostgreSQL READ ONLY, sin iniciar sesión, usar JWT, cambiar credenciales ni escribir registros: UndefinedFunction (42883), seguida de InFailedSqlTransaction (25P02).

El recuento de empleados enrutados aplicaba DISTINCT al modelo User completo, incluidas sus columnas JSON. PostgreSQL no ofrece esa comparación de igualdad para JSON. La excepción se absorbía en _safe_count y dejaba la transacción inutilizable para el resto del ingreso.

Se proyecta sólo User.id antes del JOIN, filtros y DISTINCT. Así el recuento sigue deduplicando empleados con varias categorías y conserva los límites de tenant y tipo de categoría. No cambia permisos, validación de Clerk, contraseñas, tokens ni esquema.

Tres regresiones comprueban proyección escalar, un empleado con dos categorías y rechazo de asociaciones de otro tenant/tipo. El caso de proyección falló antes del cambio (52 columnas frente a una). Después, la misma proyección completa contra PostgreSQL real terminó sin errores y sin escrituras. Las pruebas y el despliegue efectivo se registran por revisión en el PR; este documento no acredita por sí solo un login exitoso ni el corte de producción.

Los avisos de scripts de extensiones del navegador no se usaron como explicación de un HTTP 500. El 503 de arranque observado en rubros es un resultado separado: un retry posterior de esa consulta obtuvo 200. No se garantiza disponibilidad de toda la plataforma a partir de ese único endpoint.
