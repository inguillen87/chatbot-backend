# Navegación administrativa: configuración real y aceptación

Continúa #2794 / #1761 sin cambiar servicios backend, modelos, datos o permisos.
El frontend fijado para este corte es d88730d93593305c1fb26d5aa7e37f2af30054bc.
La nueva pareja necesita su propia CI; la evidencia anterior de frontend1e8b8104
permanece histórica y no se modifica ni se atribuye a este nuevo candidato.

En los recorridos anteriores el harness añadía un bypass /admin que no existía
en la configuración Vite de desarrollo. Se corrigió Vite en el frontend y se
eliminó ese override del runner. Ahora abrir y recargar /admin/encuestas debe
funcionar con el proxy del repositorio, no gracias a un parche de la prueba.
Los únicos parámetros locales son host/puerto/cache y el destino loopback del
backend desechable. Las solicitudes API siguen al servidor Flask original.

El renderer de la aplicación, login, cookies, búsqueda, confirmación, cierre y
borrado no se simulan. Se mantienen los cuatro recorridos, la inyección de fallo
de lectura exclusivamente local, la comprobación de una mutación por escenario
y la comprobación final de persistencia. results.json identifica explícitamente
actualFrontendViteConfig=true y testProxyOverride=false.

La implementación de enrutamiento es la de affd2937; d88730d9 sólo corrige un
supuesto del test HTTP sobre el204 de preflight CORS y su registro documental.
La configuración CORS del producto no se alteró. Se fija el head final para no
presentar una prueba de una revisión distinta como aceptación de este candidato.

La corrección sólo cambia el enrutamiento del servidor de desarrollo/QA Vite,
no los rewrites desplegados de Vercel. No representa nueva funcionalidad de
negocio, publicación, PWA física ni certificación de concurrencia PostgreSQL.
Los resultados exactos por SHA y run se registran en ambos PR tras finalizar.
