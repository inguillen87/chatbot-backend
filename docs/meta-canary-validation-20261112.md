# Validación offline de Meta — 12 de noviembre de 2026

Esta evidencia verifica código y transacciones de prueba. No acredita estado de Production, credenciales reales ni entrega de WhatsApp.

## Paso integrado de CI

| Control | Resultado |
| --- | --- |
| Paso | Verify dormant Meta callback, claim and CRM circuit offline |
| Casos | 531 aprobados; 0 fallos, 0 errores, 0 omitidos |
| Archivos objetivo | 24 |
| Duración | 82,72 segundos |
| Commit local probado | 7b7a2e18fcd7735943de59215e224d6d61d2a7f8 |
| Árbol probado | 956417c9fa6a2e0c49fa9b6ed223929dda0148ad |
| Resultado del guard | passed=true |
| Fuentes / blobs | Iguales a HEAD y sin cambios durante las pruebas |
| Entorno vigilado | Vacío antes y después |
| XML SHA-256 | d4930d66fe84f8e5bc2209222351e5bc9c381700d61b17ab8bbb76d9f008374f |

Se ejecutó el Python exacto del paso en `.github/workflows/tdf-accessible-channel.yml`, con `prepare_process` y proveedores de red bloqueados. La primera corrida aprobó los casos pero detectó una variable sintética persistente al importar una fixture. Se quitó esa única línea y se repitió el paso completo; no se relajó el guard.

## PostgreSQL real descartable

PostgreSQL18.6, clúster nuevo con TLS y acceso sólo por loopback. Los cuatro casos del runner `tests/run_meta_credential_admin_postgres.py` pasaron:

- `real_table_lock_jsonb_cas_one_winner`.
- `revoked_clerk_session_rejected_before_apply`.
- `real_session_revocation_waits_for_credential_commit`.
- `real_global_authority_waits_through_http_commit`.

Se usaron autenticación, sesiones, SQL/JSONB, CAS y lease originales. Sólo Graph y la comprobación de hosting fueron sintéticos. Se observaron bloqueos físicos mediante `pg_blocking_pids`, distintos backends y un único commit/audit ganador. El clúster quedó detenido y sus claves/contraseña efímeras se retiraron. No se accedió a Neon.

## Correspondencia con la entrega

La entrega agrega únicamente documentación y el runner PostgreSQL después del paso integrado. Todos los archivos incluidos en las verificaciones de ambas pruebas coinciden byte a byte con la entrega. Los hashes del tramo PostgreSQL son:

| Archivo | SHA-256 |
| --- | --- |
| `middleware/cutover_writer_fence.py` | `d2a70211635009c7567b071fd3cc428fab160aed0f3461e8e8cb6eaad7d0b7c8` |
| `routes/meta_credential_admin.py` | `baf91a084808910e39ffde112ffadca6a0d989a0351c36b8450f13e36171e574` |
| `services/auth_session_lifecycle.py` | `052fe768456b2e3cb6209ae751f9d524610181cbf201f9bd3ed4eed6fe771fe9` |
| `services/global_writer_authority.py` | `f072a99308029ee160610d98f588c9b10a8b75b47c5f35d22145a9f6ecafa39f` |
| `services/meta_credential_admin_runtime.py` | `cd263bafcd0dfc34c95218f126eeeedeba1a7c64e93fdac56b64dcca34428e4d` |
| `services/meta_credential_operations.py` | `34882e952b7d77fc51a4c13452e3d2ea30edebf60345c32d6f6276164f4ec2f4` |
| `tests/run_meta_credential_admin_postgres.py` | `46dd723865db46f98f40041d092f5eb8879faf57f9ebfc3ed98614d2aa27fda0` |
| `utils/meta_credential_http.py` | `8b19492d3a1d2f4ee17bf389ae691861e0a45ed2fb24ae48a1b4b4b5ed75e592` |

El estado del CI remoto se verifica por separado sobre el head remoto. Los resultados locales no se presentan como checks de GitHub ni como comprobación de mensajes reales.
