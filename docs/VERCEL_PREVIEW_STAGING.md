# Staging reproducible del backend Vercel

La comprobacion del candidato e1d51bed detecto que /api/version respondia con
be4bef25dba020af662f1fb12f7017988893dc15. El comando manual habia usado
BACKEND_VERSION; el runtime da prioridad a variables de plataforma y exige
CHATBOC_DEPLOYMENT_REVISION para releases CLI/container. No se cambio el
verificador para aceptar una revision incorrecta ni la precedencia del runtime.

Usar `python -m scripts.stage_vercel_preview --revision <SHA_COMPLETO>` para
validar sin desplegar. Agregar `--deploy --output <RECIBO_FUERA_DEL_REPO>` crea
un solo candidato Preview. Se comprueban revision exacta, arbol limpio,
repositorio y proyecto/equipo correctos, framework Container e inventario de
archivos antes de subir. La herramienta no lee valores de credenciales.

La revision se fija mediante CHATBOC_DEPLOYMENT_REVISION. El bloqueo de
escrituras queda habilitado; notificaciones y sincronizacion/inicializacion de
esquema se deshabilitan explicitamente para este candidato. No hay opciones
para promover, modificar dominios, cambiar planes ni retirar el bloqueo.
El recibo inicial no afirma READY, autenticacion o autorizacion de cutover.

Despues ejecutar `scripts.verify_migration_runtime` contra el host inmutable
con la misma revision. Exigir version correcta antes/despues y DB/Redis listos.
Ante un resultado ambiguo de upload, inspeccionar Vercel antes de reintentar;
la herramienta nunca reintenta automaticamente una creacion de deployment.

Las regresiones se ejecutan con `python -m unittest tests.test_stage_vercel_preview -v`
y forman parte de Migration runtime gate. No requieren secretos, una base de
clientes ni llamadas a Vercel. El nuevo helper no altera endpoints del producto.
