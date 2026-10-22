import json

from django.core.management.base import BaseCommand, CommandError

from autenticacion.predictive_data_quality import auditar_datos_predictivos


class Command(BaseCommand):
    help = 'Audita la calidad semanal de los datos históricos predictivos por departamento.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=90,
            help='Número de días históricos a revisar (por defecto: 90).',
        )
        parser.add_argument(
            '--json',
            action='store_true',
            help='Devuelve el resultado en formato JSON.',
        )

    def handle(self, *args, **options):
        days = options['days']
        if days <= 0:
            raise CommandError('--days debe ser mayor que cero.')

        result = auditar_datos_predictivos(days=days)
        if options['json']:
            self.stdout.write(json.dumps(result, ensure_ascii=False))
            return

        self.stdout.write(
            f"Auditoría predictiva: {result['desde']} a {result['hasta']} "
            f"({result['semanas_esperadas']} semanas esperadas)."
        )
        for dataset in result['datasets']:
            estado = dataset['estado']
            estilo = {
                'OK': self.style.SUCCESS,
                'ADVERTENCIA': self.style.WARNING,
                'CRITICO': self.style.ERROR,
            }[estado]
            self.stdout.write(estilo(f"[{estado}] {dataset['departamento']}"))
            self.stdout.write(
                f"  Fuente: {dataset['fuente']} | registros: {dataset['registros']} | "
                f"semanas cubiertas: {dataset['semanas_cubiertas']}/"
                f"{dataset['semanas_esperadas']} | inválidos: {dataset['registros_invalidos']}"
            )
            for mensaje in dataset['mensajes']:
                self.stdout.write(f"  - {mensaje}")
            if dataset['semanas_faltantes']:
                self.stdout.write(
                    f"  Semanas faltantes: {', '.join(dataset['semanas_faltantes'])}"
                )

        self.stdout.write(
            f"Resumen: {result['correctos']} correctos, "
            f"{result['advertencias']} con advertencias, "
            f"{result['criticos']} críticos."
        )
