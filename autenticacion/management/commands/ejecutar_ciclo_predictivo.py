import json

from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder

from autenticacion.ml_analytics import ejecutar_ciclo_predictivo
from autenticacion.models import UsuarioERP


class Command(BaseCommand):
    help = 'Ejecuta el ciclo operativo de predicciones, evaluación y monitoreo de deriva.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--usuario',
            default='',
            help='Usuario que quedará asociado a los registros predictivos.',
        )
        parser.add_argument(
            '--sin-evaluar',
            action='store_true',
            help='Registra nuevas predicciones sin evaluar periodos vencidos.',
        )
        parser.add_argument(
            '--json',
            action='store_true',
            help='Devuelve el resultado en formato JSON.',
        )

    def handle(self, *args, **options):
        username = (options.get('usuario') or '').strip()
        usuario = None
        if username:
            usuario = UsuarioERP.objects.filter(username=username, activo=True).first()
            if usuario is None:
                raise CommandError(f'No existe un usuario ERP activo con username "{username}".')

        result = ejecutar_ciclo_predictivo(
            usuario=usuario,
            evaluar=not options.get('sin_evaluar', False),
        )
        if options.get('json'):
            self.stdout.write(json.dumps(result, cls=DjangoJSONEncoder, ensure_ascii=False))
            return

        self.stdout.write(self.style.SUCCESS('Ciclo predictivo ejecutado correctamente.'))
        self.stdout.write(f"Versión del modelo: {result['version_modelo']}")
        self.stdout.write(f"Registros: {result['registrados']}")
        self.stdout.write(f"Evaluaciones: {result['evaluadas']}")
        salud = result['salud']
        self.stdout.write(
            f"Monitoreo: {salud['predicciones']} predicciones, "
            f"{salud['evaluadas']} evaluadas, "
            f"{salud['pendientes_vencidas']} pendientes vencidas, "
            f"deriva={'sí' if salud['deriva_detectada'] else 'no'}."
        )
