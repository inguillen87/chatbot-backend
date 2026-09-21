# Recuperación: aceptación integrada aislada

Continúa backend #2791 y frontend #1758 sin abrir otra variante de producto.
El frontend se fija a 66ab05294d25bf3dfffe436cd98a75bf0c8c92f3; el backend
corresponde al head que ejecuta la CI. No se modifican dependencias ni despliegues.

Se reutiliza sin cambios el helper de aceptación de la línea de configuración
(blob 06ca09dc7c7c3889939b7a88ea20baaa67b4d1df). El proceso limpia el entorno,
impide cargar .env, bloquea redes externas y crea SQLite/cuentas desechables.
Se ejecuta create_app completo, con modelos, middleware, login y cookies originales.
No se omiten decoradores de autenticación ni se copian handlers al test.

Doce casos HTTP: configuración pública exacta, hints, SQL denegado, HEAD,
sesión preservada, acceso privado anónimo/ajeno rechazado, contraseña incorrecta,
error de archivo, contrato de versión, métodos rechazados y control de pruebas.
La falla de DB se inyecta antes de ejecutar SQL. El test exige que la cadena
completa ni siquiera lo intente para la configuración pública anónima.

Cuatro recorridos Chromium conectan el componente real al servidor Flask completo
por proxy Vite: servicio no disponible, revisión incompatible, red offline con
intento manual y posterior reconexión, y espera lenta. No hay respuestas de API
o login simuladas. La red usa la emulación de Chromium y los fallos de servicio
se inyectan únicamente en el harness local; no son cortes físicos ni incidentes.
Se conserva la sesión de contraseña real y se revisan textos publicados, edición,
geometría, GET sin credenciales y ausencia de reenvíos durante la recuperación.

El fixture monta el componente de recuperación y un formulario sintético, no el
router completo del CRM. Quedan fuera MFA, PWA instalada, hardware, proveedores,
cuentas institucionales y aceptación sobre una URL desplegada. La configuración
se compara con el JSON efectivamente servido por el backend, no con un mock HTTP.

CI verde del frontend y deployment Vercel son evidencias distintas. El estado
Vercel del head 66ab052 fue failure al reanudar; no se accedió a sus logs privados
por 403. No se atribuye una causa al build sin esa evidencia. Desktop Commander
sigue pausado por cuota. No hubo publicación, cambios de aliases o datos de clientes.
Los resultados definitivos de esta aceptación se registran al finalizar la CI.
