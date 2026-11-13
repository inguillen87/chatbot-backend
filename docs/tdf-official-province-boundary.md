# Límite provincial oficial de Tierra del Fuego

## Fuente y bytes publicados

FUENTE: Instituto Geográfico Nacional de la República Argentina.

El archivo `data/tenants/tierra-del-fuego/official_province_boundary.geojson` es la respuesta **sin modificar** de [esta consulta oficial del IGN](https://ide.ign.gob.ar/geoservicios/rest/services/servicio_fondos/MapServer/0/query?where=IN1%3D%2794%27&outFields=OBJECTID%2CObjeto%2CIN1%2CFNA&returnGeometry=true&outSR=4326&f=geojson&maxAllowableOffset=0.0005&geometryPrecision=6). Se solicitó `IN1='94'`, referencia EPSG:4326, seis decimales y generalización de servidor `maxAllowableOffset=0.0005` grados. No se simplificó ni recortó la geometría en Chatboc.

- Servicio: `servicio_fondos/MapServer/0`.
- Entidad: `OBJECTID=361`, `IN1=94`, `Objeto=Provincia`.
- Nombre publicado: Tierra del Fuego, Antártida e Islas del Atlántico Sur.
- Extracción: `2026-11-13T04:15:28.456969Z`. La entidad no informa una fecha de actualización; no se sustituye por la fecha de extracción.
- Tamaño: 1.753.050 bytes; 1.332 polígonos/componentes, 73.487 posiciones.
- SHA-256: `83e543b765d0879f1743892fb0267656f83816efaa3ea326dcb1f3a61df8930b`.
- Extent publicado: oeste −74,029854; sur −90; este −25,023145; norte −51,000057.

El [catálogo del IGN](https://www.ign.gob.ar/NuestrasActividades/InformacionGeoespacial/CapasSIG) describe sus capas oficiales y sistemas de referencia. La [metadata del servicio](https://ide.ign.gob.ar/geoservicios/rest/services/servicio_fondos/MapServer/0) identifica los campos y la geometría provincial. La copia mantiene los metadatos y atributos recibidos; `.gitattributes` desactiva la conversión de finales de línea para conservar el hash entre Windows y Linux.

Los [términos de uso del IGN](https://riesgo.ign.gob.ar/pdf/Terminos-y-condiciones-de-uso-IGN.pdf) exigen atribución, conservación de información y metadatos y disponibilidad libre de los datos, sin implicar patrocinio del organismo. El uso comercial está previsto para productos derivados que emplean la información como insumo. La copia oficial se distribuye íntegra en el repositorio con su referencia y fecha de extracción.

## Alcance y precisión

El snapshot conserva la provincia completa según la representación del IGN, incluidas las islas y el sector antártico publicados por ese organismo. No se presenta la unión de tres departamentos de la Isla Grande como si fuera toda la provincia.

La generalización de 0,0005 grados es una tolerancia de visualización del servicio oficial. No equivale a un límite catastral de precisión centimétrica ni a una geocodificación exacta de una dirección. La contención del mapa usa el polígono publicado, no el rectángulo de su extent; por ejemplo, Porvenir y Punta Arenas quedan fuera aunque estén dentro de ese rectángulo.

Se descartó para el runtime la descarga WFS `ign:provincia`, feature `provincia.69`: tenía 89.993.907 bytes, 3.269.176 posiciones y una coordenada de latitud −90,00000001 que el validador estricto rechaza. Su hash observado fue `ec0fb7ccc6dd6e23b145c921c226ad1ce0b0ca2dc12986106393fb0e491abc83`. No se relajaron los rangos geográficos ni se corrigieron silenciosamente esos bytes. La fuente utilizada es la consulta ArcGIS del propio IGN identificada arriba, con coordenadas válidas.

## Contrato de ejecución

`geo.json` sólo se selecciona para el slug exacto `tierra-del-fuego`. El adaptador `ign.province.v1` verifica el hash del archivo, el tipo FeatureCollection, una sola entidad, su identificador/código/nombre provincial, el publicador, el endpoint y los parámetros oficiales, y geometría Polygon/MultiPolygon válida. Identidad, procedencia, hash o geometría inconsistentes dejan la contención sin verificar. Junín conserva su adaptador departamental y su snapshot anterior.

El descriptor expone `containment_method=point_in_polygon`, `containment_verified=true` y `truth_boundary=official_province_boundary_generalized` sólo después de esas comprobaciones. La geometría sigue siendo necesaria para clasificar cada coordenada persistida: incorporar el archivo no crea casos ni coordenadas ni autoriza a otra organización.

## Encuadre del mapa

El frontend actual obtiene `fitToBounds` de los puntos reales visibles y ya aplica ese encuadre al cambiar filtros. El backend genera sus presets desde los bounds de esos puntos. `jurisdiction.bounds` conserva el extent provincial para el contrato de contención y no reemplaza la cámara del mapa vivo. Por ello no hace falta excluir Antártida o islas del alcance para mostrar reclamos de Ushuaia, Río Grande o Tolhuin a escala local. Esta conclusión proviene de revisión de ambos consumidores; la aceptación visual con sesión de TDF corresponde al despliegue posterior.

Evidencia del consumidor revisado en frontend `ef7bc14001690332aaa26ce348c40aa29f332822`: `src/features/analytics/PremiumTerritoryMap.tsx` define `showLiveMap` en 2516 y lo condiciona a puntos reales; genera `liveMapBounds` en 2620–2623 y lo pasa en 3517. `src/components/MapLibreMap.tsx` aplica ese ajuste en 2417–2464. Con cero puntos no monta ese mapa vivo: conserva el estado vacío o la representación agregada permitida por sus controles de privacidad; no reemplaza la cámara por el extent antártico. En backend, `services/operational_intelligence.py::_heatmap_viewport_presets` recibe los bounds calculados a partir de los puntos y utiliza `tenant_region_empty` si no hay puntos, hotspots ni direcciones para revisar. En 4538–4545 se separa la colección oficial del descriptor público: la respuesta transporta la geometría una vez, en `geo_layers.boundaries`.

La separación de adaptadores para mantener el contrato departamental de Junín retoma la propuesta revisada con Claude en `b8a34dd5f91067ed6ccd430ce1621a64e1a84fa4`. La entrega provincial usa un adaptador IGN tipado; no incorpora el matching genérico ni transforma tres departamentos en una provincia completa.

## Validación y publicación

Las pruebas cubren la copia exacta del snapshot, pertenencia y exclusión reales, mismatch de autoridad/código/entidad/nombre, origen o parámetros distintos, hashes alterados, geometrías inválidas, conjunto inesperado de entidades, aislamiento por slug y regresión de Junín. El gate existente de empaquetado territorial ejecuta ambos conjuntos.

Resultados locales: 53 pruebas de contención, identidad y empaquetado pasaron en 1,57 s; luego se agregó y ejecutó una prueba del contexto de Vercel, que pasó en 0,22 s. Los cuatro casos HTTP focalizados (TDF y regresiones de Junín) pasaron en 6,19 s. Una única repetición instrumentada del caso TDF midió la ruta en aproximadamente 0,66 s: HTTP 200, 1.844.434 bytes, tres puntos visibles (Ushuaia, Río Grande y Tolhuin), uno excluido (Punta Arenas), y encuadre calculado sólo con las tres ciudades. Es evidencia funcional con un conjunto pequeño, no una prueba de carga.

Vercel usa `framework=container`; `.vercelignore` y `.dockerignore` conservan ambos conjuntos de datos territoriales. Tras `COPY . .`, `Dockerfile.vercel` ejecuta una comprobación con biblioteca estándar de Python: lee los dos `geo.json`, exige sus snapshots y compara su SHA-256. La ausencia o corrupción impide completar la imagen. Se ejecutó ese mismo comando sobre archivos exportados localmente: pasó con los originales y falló al quitar o corromper TDF. No se construyó una imagen local porque este entorno no dispone de Docker. El build real de Vercel queda obligado a comprobar los archivos dentro del contexto que recibió.

No hay migraciones ni escrituras de datos. El cambio se entrega como código y datos versionados; la publicación y la comprobación autenticada en producción se registran por separado en el PR.
