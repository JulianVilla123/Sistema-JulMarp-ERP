# Fase 8 — Despliegue seguro y respaldo

## Estado actual

- La base activa de desarrollo es SQLite local.
- Supabase permanece desactivada mediante `USE_SUPABASE_DB=false`.
- PostgreSQL está preparado, pero no se activa sin una URL y autorización explícitas.
- No se deben subir `.env`, contraseñas ni respaldos al repositorio.

## Comprobaciones locales

```powershell
venv\Scripts\python.exe manage.py check
venv\Scripts\python.exe manage.py makemigrations --check --dry-run
venv\Scripts\python.exe manage.py respaldar_base_local --json
venv\Scripts\python.exe manage.py validar_despliegue_predictivo --json
```

El respaldo se guarda en `backups/` y se verifica con `PRAGMA integrity_check`.

## Preparar producción

En el entorno productivo se deben configurar variables fuera del repositorio:

```text
DJANGO_DEBUG=false
DJANGO_SECRET_KEY=<clave-secreta-aleatoria>
ALLOWED_HOSTS=<dominio-produccion>
CSRF_TRUSTED_ORIGINS=https://<dominio-produccion>
USE_POSTGRES_DB=true
DATABASE_URL=postgresql://<usuario>:<clave>@<host>:5432/<base>
```

La migración a PostgreSQL debe hacerse primero en una base de prueba, ejecutar `migrate`, importar un respaldo validado y comprobar el ciclo predictivo. La SQLite local se conserva como respaldo de desarrollo.

## Criterio de cierre

La fase queda lista para producción cuando `validar_despliegue_predictivo` reporte `APTO_PRODUCCION`, `check --deploy` no tenga advertencias bloqueantes, los archivos estáticos estén recopilados y la base PostgreSQL de prueba haya pasado migraciones, pruebas y auditoría de datos.
