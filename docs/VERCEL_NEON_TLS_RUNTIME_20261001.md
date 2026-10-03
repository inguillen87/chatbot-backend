# Conexión efectiva del candidato Vercel a Neon

Base: `7ba03556c397882ab3520d8a5dd66fa9d63aedfc`.

Se guardaron las dos conexiones sensibles exclusivamente en la rama Preview del candidato. El despliegue CLI anterior tenía metadatos de rama, pero no gitSource y no resolvía TDF; no se aceptó como prueba de estar usando la base recuperada. El siguiente despliegue se creó desde el repo, rama y SHA exactos mediante la API oficial de Vercel.

El despliegue Git construyó correctamente, pero la prueba HTTP de readiness falló y la resolución de TDF devolvió 500. El log del runtime identificó psycopg2.OperationalError y «SSL error: certificate verify failed» contra el hostname pooled correcto de Neon. No se atribuyó el resultado a una contraseña del usuario ni se trató READY como aceptación funcional.

## Corrección

Las dos fábricas de SQLAlchemy ahora resuelven la intención explícita `sslrootcert=system` al paquete de CA ya fijado en requirements (certifi). Los drivers binarios pueden utilizar una ubicación de CA predeterminada distinta a la del contenedor. Se conservan la validación del certificado y del hostname con `sslmode=verify-full`; no se reintenta con require, verify-ca o disable.

No se cambian credenciales, servidor, base, roles, opciones ajenas ni rutas CA personalizadas. Parámetros duplicados o modos más débiles combinados con system son rechazados. La ausencia del paquete de confianza falla con un código fijo, sin imprimir la conexión. No se agregan dependencias.

Nueve regresiones unitarias nuevas. Aprobaron 36 pruebas conjuntas de configuración, CA y arranque, más 22 HTTP con la aplicación aislada. La comprobación final contra Vercel/Neon debe ejecutarse sobre la nueva revisión y registrarse por separado en el PR; estos resultados locales no sustituyen esa prueba.

También se reincorporó el corpus privado preservado de TDF en la base candidata, como operación de recuperación trazada con actor nulo (no simulando una sesión nominal). Versión 1.2.0, 40 nodos y dos fuentes, estado privado. No cambió el API público ni la autoridad de escritura; no se publica contenido del cliente en este documento.
