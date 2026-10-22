import json
import sqlite3
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Crea y verifica un respaldo de la base SQLite local activa.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--output-dir',
            default='backups',
            help='Directorio relativo al proyecto donde se guardará el respaldo.',
        )
        parser.add_argument('--json', action='store_true', help='Devuelve el resultado en JSON.')

    def handle(self, *args, **options):
        database = settings.DATABASES['default']
        if database['ENGINE'] != 'django.db.backends.sqlite3':
            raise CommandError('La base activa no es SQLite; este comando solo respalda la base local.')

        source_path = Path(database['NAME']).resolve()
        if not source_path.exists():
            raise CommandError(f'No existe la base local: {source_path}')

        output_dir = (settings.BASE_DIR / options['output_dir']).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        backup_path = output_dir / f'{source_path.stem}-{timestamp}.sqlite3'

        with sqlite3.connect(source_path) as source:
            integrity = source.execute('PRAGMA integrity_check').fetchone()[0]
            if integrity != 'ok':
                raise CommandError(f'La base local no pasó integrity_check: {integrity}')
            with sqlite3.connect(backup_path) as target:
                source.backup(target)
                backup_integrity = target.execute('PRAGMA integrity_check').fetchone()[0]

        if backup_integrity != 'ok':
            raise CommandError(f'El respaldo no pasó integrity_check: {backup_integrity}')

        result = {
            'estado': 'OK',
            'base_origen': str(source_path),
            'respaldo': str(backup_path),
            'integridad_origen': integrity,
            'integridad_respaldo': backup_integrity,
            'tamano_bytes': backup_path.stat().st_size,
        }
        if options['json']:
            self.stdout.write(json.dumps(result, ensure_ascii=False))
        else:
            self.stdout.write(self.style.SUCCESS('Respaldo local creado y verificado correctamente.'))
            self.stdout.write(f"Archivo: {backup_path}")
            self.stdout.write(f"Integridad: {backup_integrity}")
