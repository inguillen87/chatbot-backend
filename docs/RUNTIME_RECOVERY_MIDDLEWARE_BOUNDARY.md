# Límite del contexto de tenant en configuración pública

La revisión P2 de #2791 detectó correctamente una limitación de las diez pruebas
iniciales: registrar sólo config_bp no ejecutaba el middleware de tenant que usa
la aplicación completa. Por lo tanto esas pruebas no demostraban una consulta
independiente de la base durante la resolución global de organización.

Se comprobó la revisión c1fa43dc por ruta: app.py importa tenant_middleware desde
middleware/__init__.py, que exporta middleware/tenant_context.py. La aplicación
lo registra mediante tenant_middleware(app). El cambio acotado evita resolver
una organización únicamente cuando Flask ya identificó el endpoint exacto
config_bp.get_runtime_recovery_ui. No es una excepción al prefijo /api/config.
Los campos de tenant que pudiera dejar un hook anterior se limpian. Los otros
endpoints conservan el comportamiento previo; no se modifica autorización.

Ocho pruebas nuevas registran el hook real antes del blueprint real. Un spy
estricto comprueba que no se invoque el resolver con GET/HEAD, hints de tenant
o errores del archivo de configuración. También comprueban limpieza del contexto,
la continuidad del resolver para rutas cercanas/anteriores y una denegación403.
Las dependencias de importación de modelos/resolvers están aisladas; no se afirma
una aceptación de create_app completo, de la base remota o de una sesión real.
El código del hook y del blueprint no se copian dentro del test.

El JSON público permanece idéntico, por lo que el frontend #1758 / 66ab0529 no
necesita reconstruir su fixture o alterar el contrato. La publicación coordinada
continúa pendiente; no se han cambiado Render/Neon, cuentas o WhatsApp.
