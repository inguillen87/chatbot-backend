# AGENTS.md - Instrucciones y Notas para Agentes IA

Este documento proporciona información importante para los agentes IA que trabajan con este codebase.

## Consideraciones de Seguridad

### Token de Empresa para Widget Embebido

**Contexto:** La funcionalidad de widget de chat embebido permite a las PYMEs y Municipios integrar el chatbot en sus propios sitios web. Para que el widget sepa a qué empresa pertenece, utiliza un token.

**Implementación Actual:**
*   El token que el widget embebido utiliza para identificar a la empresa (PYME/Municipio) es el `User.token` del usuario administrador de dicha entidad.
*   Este token se envía en las cabeceras HTTP (ej. `Authorization`, `X-Token`, o preferiblemente `X-Entity-Token`) en las solicitudes desde el widget al backend.
*   El backend valida este token y carga el `User` correspondiente como el `owner_user` (la empresa propietaria del widget).

**Advertencia de Seguridad Importante:**
*   **Sensibilidad del Token:** El `User.token` del administrador es el mismo token que se utiliza para la autenticación en la plataforma principal de Chatboc. Por lo tanto, otorga los mismos privilegios que el usuario administrador.
*   **Riesgo de Exposición:** Si este token se incrusta directamente en el código frontend (HTML/JavaScript) del sitio web donde se embebe el widget, queda expuesto y puede ser obtenido por cualquier persona que inspeccione el código fuente de la página.
*   **Potencial Abuso:** Un token de administrador expuesto podría ser utilizado por un actor malicioso para acceder a las APIs de gestión de la empresa con privilegios de administrador. Esto podría incluir ver/modificar datos del perfil de la empresa, acceder a información de clientes/tickets, o interactuar con otras funcionalidades administrativas.

**Recomendaciones para Empresas Integradoras (Usuarios de Chatboc):**
1.  **Tratar el Token como Secreto:** El token de empresa debe ser tratado con el máximo nivel de confidencialidad.
2.  **Evitar Incrustación Directa:** **NO SE RECOMIENDA INCRUSTAR EL TOKEN DIRECTAMENTE EN EL CÓDIGO HTML/JS PÚBLICO.**
3.  **Método de Integración Seguro (Recomendado):**
    *   La empresa que integra el widget debería cargar el token desde una variable de entorno en su propio servidor.
    *   Su backend debería obtener este token y pasarlo de forma segura al widget cuando se inicializa en el frontend del cliente, o actuar como un proxy para las solicitudes del widget al backend de Chatboc.
4.  **Uso de Cabecera `X-Entity-Token`:** Al configurar el widget para que envíe el token, es preferible utilizar la cabecera `X-Entity-Token`.

**Recomendación para el Desarrollo Futuro de Chatboc (Mediano/Largo Plazo):**
*   **Implementar Tokens de API Específicos para Widgets:**
    *   Desarrollar una funcionalidad que permita a los administradores de PYMEs/Municipios generar tokens de API dedicados exclusivamente para el uso del widget.
    *   Estos tokens deberían tener permisos intrínsecamente restringidos:
        *   Suficientes para identificar a la empresa (`owner_user`).
        *   Permitir operaciones de chat para los clientes/ciudadanos de esa empresa (enviar mensajes, registrarse/loguearse bajo esa empresa).
        *   **NO DEBERÍAN** permitir el acceso a APIs de gestión de la cuenta de la empresa (actualización de perfil de empresa, gestión de empleados, acceso a datos agregados sensibles, etc.).
    *   Estos tokens deberían ser gestionables (crear, ver, revocar) desde el panel de administración de la empresa.

**Agentes IA:**
*   Al trabajar en funcionalidades relacionadas con el widget embebido o la autenticación de entidades, tened presente esta sensibilidad.
*   Si se implementan nuevas rutas protegidas por `token_requerido` que acceden a datos o funciones administrativas, asegurar que existan chequeos de rol adicionales y considerar el contexto de si la llamada podría originarse desde un widget con un token de administrador.

## Otras Notas
*   (Vacío por ahora, añadir más notas según sea necesario)
