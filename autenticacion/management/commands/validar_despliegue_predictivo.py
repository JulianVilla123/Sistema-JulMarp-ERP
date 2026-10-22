import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from autenticacion.ml_analytics import VERSION_MODELO_ACTUAL, monitorear_salud_predictiva


class Command(BaseCommand):
    help = 'Valida la preparación del ERP para desarrollo o despliegue productivo.'

    def add_arguments(self, parser):
        parser.add_argument('--json', action='store_true', help='Devuelve el resultado en JSON.')

    def handle(self, *args, **options):
        engine = settings.DATABASES['default']['ENGINE']
        executor = MigrationExecutor(connection)
        pending_migrations = len(executor.migration_plan(executor.loader.graph.leaf_nodes()))
        health = monitorear_salud_predictiva()
        static_root = Path(settings.STATIC_ROOT)
        production_security_ready = bool(
            not settings.DEBUG
            and settings.SECRET_KEY
            and not settings.SECRET_KEY.startswith('django-insecure-')
            and settings.ALLOWED_HOSTS
        )
        database_ready = engine in {
            'django.db.backends.sqlite3',
            'django.db.backends.postgresql',
        }
        production_database_ready = bool(
            engine == 'django.db.backends.postgresql'
            and getattr(settings, 'USE_POSTGRES_DB', False)
        )
        checks = {
            'base_activa': {
                'engine': engine,
                'sqlite_local': engine == 'django.db.backends.sqlite3',
                'supabase_activa': bool(getattr(settings, 'USE_SUPABASE_DB', False)),
                'postgres_habilitado': bool(getattr(settings, 'USE_POSTGRES_DB', False)),
                'estado': 'OK' if database_ready else 'ERROR',
            },
            'migraciones': {
                'pendientes': pending_migrations,
                'estado': 'OK' if pending_migrations == 0 else 'PENDIENTE',
            },
            'seguridad_produccion': {
                'debug': settings.DEBUG,
                'secret_key_configurada': bool(settings.SECRET_KEY),
                'hosts_configurados': bool(settings.ALLOWED_HOSTS),
                'estado': 'OK' if production_security_ready else 'PENDIENTE',
            },
            'archivos_estaticos': {
                'static_root': str(static_root),
                'existe': static_root.exists(),
                'estado': 'OK' if static_root.exists() else 'PENDIENTE_COLLECTSTATIC',
            },
            'modelo_predictivo': {
                'version': VERSION_MODELO_ACTUAL,
                'validacion_tecnica': health.get('validacion_tecnica', {}).get('estado'),
                'validacion_productiva': health.get('validacion_productiva', {}).get('estado'),
                'estado': 'OK' if health.get('validacion_tecnica', {}).get('estado') == 'completa' else 'PENDIENTE',
            },
        }
        production_ready = production_database_ready and all(
            item['estado'] == 'OK'
            for key, item in checks.items()
            if key not in {'seguridad_produccion', 'archivos_estaticos'}
        ) and production_security_ready and static_root.exists()
        result = {
            'estado_entorno': 'APTO_PRODUCCION' if production_ready else 'APTO_DESARROLLO',
            'produccion_lista': production_ready,
            'checks': checks,
        }
        if options['json']:
            self.stdout.write(json.dumps(result, ensure_ascii=False, default=str))
        else:
            self.stdout.write(self.style.SUCCESS(f"Entorno: {result['estado_entorno']}"))
            for name, check in checks.items():
                self.stdout.write(f"{name}: {check['estado']}")
