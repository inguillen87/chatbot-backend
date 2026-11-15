# P5 / A1 — Evidencia de rutas del proyecto

**Estado: diseño local para revisión, 15/11/2026. No implementa el adaptador ni habilita una activación.**

Base inspeccionada: backend `1e23c54ef32b5f75b970c2715fe12f8193341cac`.
Alcance: sumar la capa mutable de rutas de Vercel a la evidencia existente de
`services/organization_domain_vercel.py::_serving` y del observador de direcciones v2.
No se hicieron llamadas autenticadas a proyectos, cambios de configuración, SQL,
dominios ni credenciales. Las únicas consultas externas fueron documentación pública.

## 1. Decisión

**Agregar una prueba independiente de las reglas LIVE del proyecto y conservar
íntegra la prueba actual de las rutas del deployment.** No sustituir
`DomainPlan.routes_sha256` por un hash de reglas del proyecto.

La evidencia que falta en una capa no queda cubierta por otra. Si el GET del
deployment devuelve `routes: null`, el resultado sigue siendo
`reviewed_route_setup_required`, aunque las reglas LIVE coincidan y el nonce
responda correctamente. A1 permite identificar y revisar la capa mutable; por sí
solo no resuelve ese bloqueo actual.

Tampoco basta un commit correcto para demostrar todas las rutas compiladas ni un
GET con nonce para demostrar el comportamiento de cada método, condición, header,
path o sesión. La aceptación de usuario, login y CRM continúan fuera de esta prueba.

## 2. Contrato oficial observado

La [API de reglas][1] permite leer `/v1/projects/{projectId}/routes`, con selección
de versión, búsqueda, filtros y comparación. La [API de versiones][2] identifica
la versión de producción con `isLive` y distingue staging. El [OpenAPI público][3]
describe varias formas de respuesta: un objeto genérico, reglas con versión,
una variante de diff y otra con información de límite. Sus indicadores de LIVE,
staging y cantidad **no son todos obligatorios en el esquema**. Por eso un HTTP
200 no alcanza para acreditar la capa. Los errores 404/410 no explican por sí
solos que no existan reglas.

Esto es documentación de contratos, no una observación del proyecto de Chatboc.
La política de aceptación siguiente es una propuesta deliberadamente más estricta.
Exigir indicadores que el esquema deja opcionales puede bloquear respuestas
legítimas; ese límite de disponibilidad es explícito. Un indicador ausente no
se convierte en `false` ni acredita una versión LIVE.

Huella del OpenAPI consultado: SHA-256
`c03e5a18d5d38b5f48c1eabf2c8a59a6d3133e638cb6ae5e01057dfe51bbbd38`,
12.357.512 bytes. Se conservó localmente el subconjunto de los dos GET para revisión.

## 3. Invariantes existentes

| Evidencia actual | Regla que se conserva |
|---|---|
| Identidad del deployment | Proyecto e ID exactos, READY, Production y `gitSource.sha` revisado. |
| Rutas del deployment | Lista completa de 1–256 entradas, digest revisado y proxy `/api` al backend esperado. `null`, ausencia o digest distinto bloquean. |
| Dominio y alias | Host exacto, propiedad verificada, mismo proyecto/deployment; sin redirección, eliminación ni microfrontends inesperados. |
| Target v2 | `AddressVercelAdapter._production` comprueba el target vigente; se mantiene. |
| Probe | Host y tenant exactos, nonce nuevo, revisión y backend esperado, DNS público, IP fijada y TLS/SNI del host; ningún redirect. |
| Autoridad y tiempo | Plan aprobado, binding vigente, alcance/roles actuales, verificación antes y después del probe y ventana total de 60 segundos. |
| Efectos | El observador sólo lee; una observación no equivale a login verificado ni a publicación autorizada. |

Referencias locales: `DomainPlan.validate`, `VercelTransport`, `_serving`, `observe`,
`AddressPlan`, `AddressVercelAdapter._production/_snapshot/observe` y
`organization_addresses.activate_address`.

## 4. Plan aprobado y digest

Versionar la ampliación como un contrato de evidencia A1, extendiendo **ambos**
planes y sus constructores/validadores. No permitir que un plan antiguo omita la
prueba nueva dentro del modo A1. No reinterpretar journals o aprobaciones anteriores.

| Dato | Semántica propuesta |
|---|---|
| `routes_sha256` existente | Exclusivamente las rutas del deployment; conserva su significado. |
| `project_routes_version_id` | ID opaco de la versión LIVE revisada. No asumir un prefijo por un ejemplo de documentación. |
| `project_routes_sha256` | Hash de la lista **completa y ordenada** de reglas del proyecto. |
| `project_routes_rule_count` | Entero explícito, sin coerción de booleanos, igual al tamaño de la lista. |
| `project_routes_last_modified` | Valor observado y revisado; no inferir segundos/milisegundos ni usarlo como reloj de frescura. |
| Alcance del proveedor | Proyecto fijo y contexto de cuenta/team del transporte, ligados al plan y sin selección ambiental. |

Usar la serialización canónica ya existente para el hash: ordenar las claves de
objetos, conservar el orden de las listas y rechazar NaN o claves JSON duplicadas.
Incluir todos los campos de cada regla, también reglas deshabilitadas y condiciones,
headers, transforms, destinos, orden y metadatos. No calcular el hash sólo sobre
`src`/`dest`, ni ordenar por ID, quitar reglas o normalizar arbitrariamente aliases.
Campos semánticos desconocidos requieren revisión del parser antes de aceptarse.

La versión y sus metadatos se vinculan al digest del plan; no se mezclan dentro
del hash de la lista. Un cambio de versión con idénticas reglas exige un nuevo
plan revisado. Un mismo ID con distinto contenido tampoco pasa.

## 5. Lectura de una captura A1

Una captura agrega dos GET de la API documentada y autenticada, **sólo en una futura
implementación autorizada**:

1. Leer `/v1/projects/{projectId}/routes/versions` con el alcance fijo. No usar
   búsqueda, filtros o diff. Exigir exactamente una versión con `isLive === true`
   e `isStaging === false`, y que su ID sea el aprobado. Exigir IDs de versiones
   únicos. Listas ambiguas, indicadores
   ausentes o strings como `"true"` son evidencia desconocida.
2. Leer `/v1/projects/{projectId}/routes?versionId=<ese ID>` sin `q`, `filter` ni
   `diff`. El parámetro se codifica desde ese ID validado; no se acepta una URL
   aportada por un usuario ni una versión histórica elegida por conveniencia.
3. Exigir el mismo `version.id` y los indicadores explícitos LIVE/no-staging en
   esta segunda respuesta. Comparar `ruleCount` y `lastModified` entre la versión
   seleccionada del índice, la versión de la respuesta de reglas y el plan. La
   cantidad debe corresponder además al tamaño real de `routes`; su digest debe
   coincidir con el plan. Una inconsistencia del índice también bloquea.
   Un GET de una versión fijada **por sí solo no demuestra** que siga vigente.
4. Aceptar únicamente una variante completa reconocida. Rechazar objetos vacíos,
   null, respuestas parciales, diff, inconsistencias de cantidad e IDs repetidos.
   Una regla con `staged: true` contradice la captura LIVE y bloquea. El objeto
   `limit` contiene la cuota `currentRoutes`/`maxRoutes`; no es un cursor ni demuestra
   truncamiento. Tampoco prueba completitud: la comprobación de cantidad y versión
   sigue siendo obligatoria.

Un proyecto con cero reglas sólo obtiene el estado «vacío verificado» si entrega
una versión LIVE inequívoca, `ruleCount` entero 0, `routes: []`, la misma observación
en ambas capturas y el hash vacío previsto en el plan. Si el proveedor representa
la ausencia mediante otra forma no documentada, el estado sigue siendo desconocido.
Nunca convertir 404, 410, falta de permisos, timeout o `version: null` en lista vacía.

El transporte futuro debe permitir únicamente esos dos GET y sus parámetros
controlados. No abrir un GET arbitrario ni heredar POST para estas rutas. Mantener
TLS, ausencia de redirecciones/reintentos/proxy, 128 KiB máximos por respuesta y
errores sin datos privados. Propuesta inicial de límite de reglas: 256; excederlo
produce bloqueo y revisión, no truncamiento silencioso.

## 6. Secuencia y cambio concurrente

1. Validar plan y alcance actuales.
2. Obtener la captura existente de deployment, dominio y alias, más la captura LIVE A1.
3. Ejecutar el probe HTTPS con nonce nuevo contra el host exacto.
4. Repetir la captura existente y la captura LIVE A1.
5. Comparar ambas con el mismo plan aprobado y revisar vigencia.
6. Emitir únicamente la observación de infraestructura.

Las dos capturas deben coincidir en deployment/source, alias, versión LIVE,
metadatos comparados, cantidad y digest. Si una promoción o edición aparece entre
ellas, se descarta la observación completa. No reintentar automáticamente con la
nueva versión ni actualizar el plan aprobado desde la respuesta del proveedor.

Con dos GET adicionales por captura, el recorrido actual completo pasaría de
7 a **11 lecturas** en v1 y de 9 a **13 lecturas** en v2, contando el probe. Se
conserva la ventana máxima de aceptación de 60 segundos, hoy comprobada al final.
Para imponer además un plazo de ejecución, la implementación futura debe pasar
el tiempo restante al transporte y al probe, cubrir resolución DNS, conexión y
lectura del cuerpo, y abortar al agotarlo. Un timeout de conexión por sí solo no
garantiza ese plazo. No ampliar TTL ni reutilizar evidencia vieja para compensar
la latencia.

Este método reduce la ventana de cambio, **no proporciona una transacción atómica
con Vercel ni detecta necesariamente un cambio A→B→A entre lecturas**. Tampoco
impide modificaciones posteriores. El recibo expresa evidencia puntual con su
vigencia; no promete inmutabilidad de una capa mutable. La activación existente
mantiene sus guardas de autoridad, revisión, colisión y frescura antes de persistir.

## 7. Resultados y aceptación

| Caso | Resultado obligatorio |
|---|---|
| Deployment con `routes: null`, aunque A1 sea válido | Bloqueo actual conservado. |
| Versión LIVE revisada, lista íntegra y ambas capturas iguales | Capa A1 verificada; aún hacen falta todas las demás pruebas. |
| Versión LIVE explícita con cero reglas y vacío revisado | Capa A1 vacía verificada, sin reemplazar rutas del deployment. |
| Sólo staging, versión histórica, cero o varias versiones LIVE | Desconocido/bloqueado. |
| Regla con `staged: true` dentro de la versión presentada como LIVE | Contradicción; no activar. |
| ID o metadatos cambian; orden o contenido difieren | Plan/evidencia no coinciden; no activar. |
| Filtro, diff, cantidad inconsistente, truncamiento o campo semántico desconocido | Evidencia incompleta; no activar. |
| 401/403/404/410, timeout o JSON ambiguo | Error de observación; nunca «sin reglas». |
| Nonce válido pero source, tenant, host o capa de rutas no coinciden | Bloqueado. |
| Dominio v2 con plan antiguo que omite A1 | No convertirlo silenciosamente a un plan A1 aprobado. |

Pruebas futuras mínimas: los casos de la tabla sobre proveedores simulados;
edición/promoción entre capturas; versión histórica con flags falsos; cuenta o
proyecto distintos; reorder, reglas disabled y condiciones ocultas por filtros;
vacío explícito frente a ausencia; límite/timeout global; preservación de todos
los tests negativos actuales. Verificar v1 y v2, y que ninguna observación haga
POST, SQL de activación o emita una capacidad privada.

## 8. Cambios futuros acotados y recibos

La implementación posterior tendría que modificar coordinadamente:

- `organization_domain_vercel.py`: plan, digest/journal y lector A1 en `_serving`.
- `organization_address_vercel.py`: `AddressPlan`, errores fijos y observación v2.
- `organization_address_runtime.py`: validación exacta del cuerpo y construcción
  del plan del operador; no aceptar observaciones fabricadas por el cliente.
- `organization_addresses.py`: `prepare_plan`, reconstrucción antes de activar,
  digest aprobado y evidencia de auditoría.
- Los tests existentes de ambas rutas de observación, sin retirar negativos.

El recibo contendría IDs, versión, hashes, cantidad, timestamps de observación y
motivos fijos de bloqueo. No publicar tokens, `s3Key`, reglas crudas, headers,
destinos con parámetros ni cuerpos del proveedor. `createdBy` aporta trazabilidad,
no autorización. No consultar el storage indicado por `s3Key`.

**P5 queda resuelto como diseño.** `_serving` y los demás archivos de producto
permanecen sin cambios. Para resolver el `null` de rutas del deployment haría falta
otra propuesta revisada: una procedencia verificable que enlace artefacto de build,
configuración compilada, deployment y source exactos. El archivo fuente
`vercel.json`, aislado de esa cadena, no basta para reemplazar la guarda actual.

## Fuentes

[1]: https://vercel.com/docs/rest-api/project-routes/get-project-routing-rules
[2]: https://vercel.com/docs/rest-api/project-routes/get-routing-rule-version-history
[3]: https://vercel.com/openapi.json

- [Vercel: lectura de reglas del proyecto][1].
- [Vercel: historial y versión LIVE][2].
- [Vercel: OpenAPI público, subconjunto GET consultado el 15/11/2026][3].
- Código local de los servicios citados, base `1e23c54ef`.
- `REPARTO-CHATGPT.md` y `verificacion-produccion-tdf.json`, entregados por Marcelo.
