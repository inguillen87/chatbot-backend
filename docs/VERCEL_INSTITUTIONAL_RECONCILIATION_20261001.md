# Candidato Vercel: conservación de las correcciones institucionales

Base Vercel exacta: `e3bee31c60e1e3318a853ac445b3ee4b0c657540`.
Correcciones operativas portadas desde: `cbef904e111e99703fcb99e5e0e00f6e9e3545d3` (#2807).

Este corte conserva el arranque de Vercel, las sesiones gestionadas por migración, Socket.IO y las protecciones de escritor del candidato. No reemplaza app.py por la versión eventlet de Render. Los tres módulos institucionales y sus dos archivos de pruebas se copian por sus blobs exactos de #2807; app.py sólo incorpora X-Chatboc-Knowledge a la lista CORS. El orden y el inventario de blueprints permanecen sin cambios.

Las correcciones preservadas son: botones/options_list del contrato existente, propietario heredado con relación inequívoca cuando falta tenant_id, rechazo ante una identidad contradictoria, cabecera de confirmación administrativa, consulta pública sin depender de la sesión o de la allowlist administrativa, y límite de importación compatible con la mayor pregunta admitida por el selector.

## Límites y comprobación

No cambia migraciones, modelos, dependencies, scripts de despliegue, Dockerfile, configuración, dominios, crons ni permisos de GitHub. Los controles de escritura y de datos del destino no se deshabilitan. No se suben documentos de TDF, backups, credenciales ni archivos de cliente.

La combinación había sido preparada localmente pero no estaba publicada en GitHub. Con el escritorio desconectado se reconstruyó esta revisión directamente desde los objetos remotos, se verificó el hash de la base y se preservó el árbol. El primer objeto intermedio tenía una diferencia involuntaria en el registro de blueprints: se detectó al comparar el commit, se descartó antes de crear una rama o PR y se sustituyó por el blob exacto esperado. No se desplegó ese objeto.

Las pruebas de la revisión local anterior no certifican este SHA. Los resultados de los workflows existentes de runtime, guía, autorización y PostgreSQL se registran en el PR después de completarse; aquí no se declara CI aprobado.

El PostgreSQL de recuperación, el ensayo local de migraciones interrumpido, la fuente final posterior al snapshot y los servicios auxiliares conservan su estado previo. La publicación de código en GitHub no migra la base, no mueve tráfico y no acredita el primer ingreso de Mauricio o Analía. La activación del endpoint previamente rechazada no se repite por una vía alternativa. MuniControl queda fuera del alcance.

Seguimiento de salida de Render: #2808. Este candidato debe utilizarse en lugar de perder los arreglos institucionales al volver a una rama anterior. Su aceptación requiere el esquema exacto ensayado, un destino identificado, un único escritor, secretos originales disponibles en plataforma, archivos/canales migrados y comprobaciones reales de los clientes.
