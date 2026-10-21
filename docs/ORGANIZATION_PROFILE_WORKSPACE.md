# Contexto de perfil por organizacion

Builder nuevo: services/organization_workspace.py.
Contrato: organization.profile_workspace.v1, campo opcional del payload autenticado
que ya construye routes/auth.py para el perfil. Base: ee52d894, PR #2781.

Identifica el tipo explicito del tenant activo, con fallback neutral. Publica seis
secciones de configuracion, ayuda sobre el sitio informativo y continuidad si existe
registro de WhatsApp. No devuelve el identificador del sender ni secretos.
No es una nueva cuenta, conexion, verificacion de dominio o permiso comercial.
Los contratos existentes de autenticacion, guardado y planes no se modifican.

El objetivo es reutilizar el perfil de una organizacion existente (como Junin) y
permitir identidad contextual para gobiernos, colegios, empresas y pymes sobre
el mismo producto. TDF sigue como piloto de evaluacion hasta completar sus gates.

Ocho pruebas nuevas del builder: tipos, alias, scope invalido/inactivo, continuidad,
no mutacion, copia independiente, ausencia de datos no necesarios y no provision.
CI extiende el job PostgreSQL anterior: 19 transacciones + 12 autoservicio + 8 perfil.
Los ocho nuevos son pruebas puras, no login/MFA ni aceptacion de una cuenta real.

No hay migraciones, cambios de usuarios/planes/dominios, llamadas al proveedor,
mensajes o activacion de Full. Publicacion y verificacion integrada se registran
por separado en el PR coordinado de frontend. No promover el backend de migracion
sobre Render sin conciliar las revisiones y los gates de cambio de plataforma.
