# Migración de SQLite a PostgreSQL

Esta guía describe los pasos para migrar la base de datos de la aplicación desde SQLite a PostgreSQL.

## Pre-requisitos
- `pip install "psycopg[binary]" alembic`
- Respaldo del archivo `instance/database.db`
- Base de datos de PostgreSQL creada y accesible
- Variable de entorno `DATABASE_URL` apuntando al servidor de PostgreSQL

## Pasos
1. **Pausar las escrituras** hacia el backend (WhatsApp y widget) y realizar un backup del archivo `instance/database.db`.
2. **Crear el esquema** en PostgreSQL:
   ```bash
   export DATABASE_URL="postgresql+psycopg://user:pass@host:5432/db"
   alembic -c migrations/alembic.ini upgrade head
   ```
3. **Migrar los datos** desde SQLite:
   ```bash
   python scripts/migrate_sqlite_to_pg.py
   ```
   El script copia las tablas respetando claves foráneas y valida que los `COUNT(*)` coincidan.
4. **Configurar Render**: establecer `DATABASE_URL` en Settings → Environment.
5. **Redeploy** manual del servicio.
6. **Pruebas de humo**: `/health`, login, creación de ticket, envío/recepción de mensaje y lectura de FAQs.

## Rollback
Si algo falla tras el cambio:
1. Detener nuevamente las escrituras.
2. Reapuntar `DATABASE_URL` al SQLite anterior.
3. Redeployar y repetir las pruebas de humo.
4. Corregir el problema y volver a ejecutar los pasos de migración.

Nunca elimine `instance/database.db` ni el esquema de PostgreSQL hasta confirmar la migración exitosa.

## Comandos
PowerShell:
```powershell
$env:DATABASE_URL="postgresql+psycopg://user:pass@host:5432/db"
alembic -c migrations/alembic.ini upgrade head
python scripts/migrate_sqlite_to_pg.py
```

Bash:
```bash
export DATABASE_URL="postgresql+psycopg://user:pass@host:5432/db"
alembic -c migrations/alembic.ini upgrade head
python scripts/migrate_sqlite_to_pg.py
```
