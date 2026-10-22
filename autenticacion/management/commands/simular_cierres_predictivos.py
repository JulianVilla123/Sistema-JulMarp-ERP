from decimal import Decimal, ROUND_HALF_UP

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from autenticacion.ml_analytics import VERSION_MODELO_ACTUAL
from autenticacion.models import RegistroPrediccion


class Command(BaseCommand):
    help = (
        'Simula cierres posteriores para validar el ciclo predictivo en desarrollo. '
        'Nunca representa resultados reales de producción.'
    )

    marker = 'SIMULACION_CIERRE_CONTROLADO_FASE6'

    def add_arguments(self, parser):
        parser.add_argument(
            '--modelo-version',
            default=VERSION_MODELO_ACTUAL,
            help='Versión del modelo que se cerrará (por defecto, la vigente).',
        )
        parser.add_argument(
            '--confirmar-demo',
            action='store_true',
            help='Confirma que la operación se ejecuta únicamente sobre datos locales de desarrollo.',
        )
        parser.add_argument(
            '--reset',
            action='store_true',
            help='Quita los resultados generados por esta simulación para repetir la prueba.',
        )
        parser.add_argument(
            '--reset-version',
            action='store_true',
            help='Quita todos los cierres de la versión indicada; solo para reiniciar una base local de demo.',
        )
        parser.add_argument(
            '--departamento',
            help='Limita la simulación a un departamento, por ejemplo Finanzas.',
        )

    def handle(self, *args, **options):
        if not options['confirmar_demo']:
            raise CommandError(
                'Esta operación solo debe ejecutarse en desarrollo. '
                'Agrega --confirmar-demo para confirmar la simulación local.'
            )

        version = options['modelo_version']
        queryset = RegistroPrediccion.objects.filter(version_modelo=version)
        departamento = (options.get('departamento') or '').strip()
        if departamento:
            queryset = queryset.filter(departamento=departamento)
        if options['reset'] or options['reset_version']:
            reset_count = 0
            with transaction.atomic():
                for registro in queryset:
                    metadatos = dict(registro.metadatos or {})
                    if not options['reset_version'] and not metadatos.get('resultado_simulado'):
                        continue
                    metadatos.pop('resultado_simulado', None)
                    metadatos.pop('tipo_cierre', None)
                    metadatos.pop('fuente_cierre', None)
                    registro.resultado_real = None
                    registro.error_real = None
                    registro.fecha_evaluacion = None
                    registro.metadatos = metadatos
                    registro.save(update_fields=[
                        'resultado_real', 'error_real', 'fecha_evaluacion',
                        'metadatos', 'fecha_actualizacion',
                    ])
                    reset_count += 1
            self.stdout.write(self.style.SUCCESS(
                f'{reset_count} cierres simulados eliminados para la versión {version}.'
            ))
            return

        ajustes = {
            'Finanzas': Decimal('0.012'),
            'Inventario': Decimal('-0.018'),
            'MFG': Decimal('0.010'),
            'QA': Decimal('0.025'),
            'Mantenimiento': Decimal('-0.020'),
        }
        now = timezone.now()
        cerradas = 0
        omitidas = 0

        with transaction.atomic():
            for registro in queryset.order_by('departamento', 'id'):
                if registro.resultado_real is not None:
                    omitidas += 1
                    continue
                if registro.valor_predicho is None:
                    omitidas += 1
                    continue

                # Variación pequeña y reproducible para probar el cálculo de error
                # sin convertir la simulación en una copia exacta del pronóstico.
                variacion_segmento = Decimal((registro.id % 3) - 1) * Decimal('0.005')
                variacion = ajustes.get(registro.departamento, Decimal('0.010')) + variacion_segmento
                resultado = registro.valor_predicho * (Decimal('1') + variacion)
                resultado = resultado.quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)
                error = abs(resultado - registro.valor_predicho).quantize(
                    Decimal('0.0001'), rounding=ROUND_HALF_UP
                )
                metadatos = dict(registro.metadatos or {})
                metadatos.update({
                    'resultado_simulado': True,
                    'tipo_cierre': self.marker,
                    'fuente_cierre': 'Datos locales de desarrollo; no son resultados reales.',
                })
                registro.resultado_real = resultado
                registro.error_real = error
                registro.fecha_evaluacion = now
                registro.metadatos = metadatos
                registro.save(update_fields=[
                    'resultado_real', 'error_real', 'fecha_evaluacion',
                    'metadatos', 'fecha_actualizacion',
                ])
                cerradas += 1

        self.stdout.write(self.style.SUCCESS(
            f'{cerradas} cierres simulados generados para la versión {version}.'
        ))
        self.stdout.write(f'Predicciones omitidas: {omitidas}.')
        self.stdout.write(
            'Estos resultados están marcados como resultado_simulado y no deben usarse '
            'como evidencia de precisión real.'
        )
