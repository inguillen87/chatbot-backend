# WL-BRAND-PALETTE: publicación versionada de colores del espacio

Base backend 86104a1c / frontend c90f73e. Se implementa un corte de marca propia
sobre los registros y autorización existentes. No es una nueva aplicación por cliente.

## Alcance funcional

Color principal, acento y activación de la paleta. GET de configuración devuelve
valores, presets, versión, revisión, hasta diez versiones anteriores y permiso efectivo.
PUT del mismo endpoint administrativo recibe organization_branding con una operación
publish o restore y expected_revision. El contrato de perfil personal no cambia.

Publicar guarda en configuracion.organization_workspace_branding y escribe auditoría
dentro de la misma transacción. La fila del tenant se bloquea; versión y autorización
se verifican otra vez. Un conflicto devuelve 412 sin sobrescribir. Restore publica una
nueva versión monotónica y no reutiliza la revisión antigua. Incluso restaurar iguales
colores genera una nueva publicación. Publicar sin cambios no crea auditoría adicional.
No se realizan migraciones de esquema ni se cambian configuraciones de proveedores.

La fuente de derecho es el plan persistido del servidor: Full y aliases existentes
enterprise, premium, municipio_full, colegio_full y pyme_full; además tenant activo,
contexto no demo y permiso de administración vigente. Una capability genérica editable
no concede este derecho. No verifica cobros en una pasarela ni activa/cobra una suscripción.
El writer fence existente prevalece; la interfaz no lo elude.

## Consumo y privacidad

La configuración pública elimina el registro privado, incluido el historial.
/api/me devuelve workspace_appearance y la activación de canales lo propaga en
organization_setup. La interfaz comprueba el tenant y los colores antes de usarlos.
Las superficies iniciales son encabezados del perfil y del centro de implementación.
No cambia colores globales, login, mensajes, dominios, PWA o aplicaciones externas.

Desactivar o perder el plan suprime la apariencia derivada sin borrar el historial.
Restaurar no concede permisos ni reabre dominios/conexiones. Nombre y logo conservan
su contrato institucional: publicar colores no guarda ediciones pendientes de ellos.
Sólo se aceptan booleanos estrictos y hexadecimales de seis dígitos, nunca CSS libre.
El cálculo de contraste blanco/negro describe muestras concretas, no certifica WCAG
para toda la aplicación. Referencia: https://www.w3.org/TR/WCAG22/#contrast-minimum .

## Pruebas

14 casos nuevos del servicio con SQL real/modelos fuente y autorización sintética.
Se ejecuta la concurrencia en PostgreSQL temporal de CI, no contra Neon de clientes.
Seis escenarios adicionales arrancan create_app y prueban el login, controles y
rutas reales con SQLite desechable. El runner SPA da Full sólo al tenant sintético
acceptance-a, nunca a una cuenta real. Los resultados finales se registran por commit.

La aceptación visual incluye previsualizar, publicar, recargar, verificar colores en
el centro de implementación, rechazar al tenant sin Full y restaurar la versión 0.
La primera ejecución detectó que el frontend olvidaba el DTO de colores al recargar;
se corrigió el mapeo de Perfil y se repitió el recorrido. Los cuerpos de API no están
simulados en esta prueba. Las cuentas y datos sí lo están.

## Pendientes de producto y operación

No completa selección granular de módulos, white label integral, alta DNS/TLS,
PWA instalada o configuración de WhatsApp. Tampoco completa arranque sin 503,
aceptación institucional en QA ni migración Render/Neon. El candidato permanece
cercado para escrituras. No se mueven aliases ni se alteran Junín o Agente Conversa.
