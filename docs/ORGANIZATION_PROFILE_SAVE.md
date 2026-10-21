# Guardado institucional versionado

Base: 99cdab59642a5819a260c3df31cddcd2285e99f6. Este corte extiende el SaaS compartido,
no crea cuentas, planes, dominios ni integraciones por cliente.

## Problema observado

El formulario institucional enviaba nombre de organización, logo, ciudad, sitio y
horarios a PUT /perfil. Ese handler delega en update_user_profile, que permite campos
personales y omite varios de los institucionales aunque devuelva éxito. Se conserva
el handler personal sin ampliar sus permisos; el formulario institucional utiliza
ahora el endpoint de configuración del tenant que ya existe.

## Contrato y persistencia

GET /api/me y GET /api/admin/tenants/{slug}/config agregan organization_profile,
contrato organization.profile_settings.v1: identidad del tenant, valores institucionales,
revisión por contenido, endpoint canónico y permiso de edición calculado en servidor.
La bandera de mantenimiento de escritores produce can_edit=false. Un campo publicado
no concede permisos, Full ni facultades sobre WhatsApp.

PUT /api/admin/tenants/{slug}/config acepta, como operación exclusiva,
organization_profile (cambios permitidos) y expected_revision. El servicio verifica
nuevamente actor, tenant activo y administración mediante el helper existente.
Bloquea tenant y filas del actor/propietario, rechaza propietarios históricos compartidos,
compara revisión y aplica la modificación con auditoría en una transacción.
Respuesta 412: versión diferente; 428: versión ausente/inválida; 403: acceso no autorizado;
503: guardado sin confirmar. No se reintentan mutaciones automáticamente.

## Cierre del sprint retomado

El bloque opcional de lectura no provoca un error global de login/perfil si un
registro historico tiene propietario ambiguo o valores no serializables. En esos
casos no publica la capacidad de guardado. No corrige datos historicos en silencio.
La respuesta de configuracion autenticada lleva Cache-Control: no-store.
Los limites de texto coinciden con las columnas institucionales existentes.

Los horarios institucionales se guardan en configuracion.organization_profile_hours;
se conserva el horario legacy del propietario/operador. El consumo por agentes,
reglas de disponibilidad o sistemas externos queda pendiente de integracion.
El hash de contenido no es un contador monotono ni una historia restaurable de marca.
La auditoria identifica actor/campos/revisiones sin duplicar valores privados.

Las pruebas usan columnas del modelo fuente y transacciones SQLAlchemy reales.
El usuario, membresia y adaptador de autorizacion son sinteticos: no certifican
sesiones de clientes ni el middleware completo. CI usa PostgreSQL 18 desechable
para concurrencia. No se conecto una base de clientes para estos tests.
El control de mantenimiento de escritores existente se conserva; can_edit no lo
sustituye. No se agregan tablas, se activan planes o se alteran canales por este corte.
