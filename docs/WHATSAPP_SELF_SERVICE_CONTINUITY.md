# Autoservicio por organización: continuidad antes que otra alta

## Entrega

`build_whatsapp_self_service` agrega metadata de presentación al contrato autenticado
existente de Tech Provider. No crea otra API, aplicación, tenant o conexión.
Distingue un número/SID ya registrado de un número solamente solicitado. Incluye
la compatibilidad con `TenantProfile.whatsapp_sender_id` para cuentas anteriores.
Un registro desconectado sigue siendo existente: no se reemplaza para corregirlo.

El contrato devuelve identidad del tenant, modo existente/nuevo, vocabulario por
tipo explícito y cuatro rutas: perfil, implementación/equipo, integraciones y
plantillas. Reutiliza las pantallas y sus controles de acceso; los enlaces NO son
entitlements, permisos ni prueba de que un módulo esté habilitado. Tipo desconocido
usa organización genérica. No infiere colegio a partir de información no confirmada.

Municipio/Gobierno/Colegio/Empresa/Pyme se resuelven por tipo de servidor, sin nombres
de clientes en componentes. Junín y TDF son casos de aceptación futuros, no cuentas
modificadas en este corte. La marca blanca Full sigue su plan transversal: identidad,
dominio y canales configurables, sin fork ni pérdida de usuarios existentes.

## Seguridad y límites

Proyección sólo de lectura: no devuelve secretos ni números en el nuevo bloque;
no habilita Full, concede roles o verifica un pago. Las integraciones mantienen sus
autorizaciones existentes. El registro de un número no certifica plantilla aprobada,
entrega real, webhook recibido ni operación productiva. No hay migraciones nuevas.

Doce pruebas puras cubren estados anteriores, número sólo solicitado, tipos, enlaces,
datos malformados, no divulgación de credenciales y ausencia de mutación. Se agregan
a la CI existente con regresiones transaccionales. No sustituyen auth/QA del sistema.
