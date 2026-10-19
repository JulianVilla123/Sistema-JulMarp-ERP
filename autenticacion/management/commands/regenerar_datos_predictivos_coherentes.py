from datetime import datetime, time, timedelta

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from autenticacion.finanzas import consolidar_costeos_produccion, generar_reporte_financiero
from autenticacion.models import CosteoProduccion, ReporteFinanciero, ReporteKPIProduccion, UsuarioERP


class Command(BaseCommand):
    help = 'Regenera datos locales coherentes para ventas, producción, materiales y Finanzas.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--periods',
            type=int,
            default=10,
            help='Cantidad de semanas a regenerar (por defecto: 10).',
        )

    def handle(self, *args, **options):
        periods = options['periods']
        if periods < 4:
            raise CommandError('--periods debe ser mayor o igual a 4 para alimentar el pronóstico.')

        today = timezone.localdate()
        current_week_start = today - timedelta(days=today.weekday())
        first_week = current_week_start - timedelta(days=7 * periods)
        last_week = first_week + timedelta(days=7 * (periods - 1))
        last_week_end = last_week + timedelta(days=4)
        usuario = UsuarioERP.objects.filter(is_superuser=True, activo=True).order_by('id').first()
        if not usuario:
            usuario = UsuarioERP.objects.filter(activo=True).order_by('id').first()
        if not usuario:
            raise CommandError('Se necesita un usuario ERP activo para generar los reportes.')

        self.stdout.write('Se regenerarán únicamente datos derivados locales de demostración:')
        self.stdout.write(f'  Periodo: {first_week} a {last_week_end}')
        self.stdout.write('  Producción: 3 segmentos por semana; materiales y costos ligados a cada orden.')
        self.stdout.write('  Finanzas: reportes KPI reconstruidos a partir de esos costeos y ventas.')

        with transaction.atomic():
            # Estos reportes son snapshots derivados; se reconstruyen después
            # de regenerar las fuentes operativas y no se eliminan movimientos.
            ReporteFinanciero.objects.all().delete()
            ReporteKPIProduccion.objects.all().delete()

            call_command('simular_semana_ml', periods=periods)
            call_command('simular_ventas_ml', periods=periods, reset=True)

            consolidar_costeos_produccion(
                usuario=usuario,
                fecha_inicio=first_week,
                fecha_fin=last_week_end,
            )

            for index in range(periods):
                week_start = first_week + timedelta(days=7 * index)
                week_end = week_start + timedelta(days=4)
                # El dashboard financiero filtra costeos por fecha_actualizacion.
                # Alineamos el snapshot al cierre de la semana productiva para
                # que rentabilidad y margen entren en el mismo periodo que OFs.
                period_timestamp = timezone.make_aware(datetime.combine(week_end, time(16, 0)))
                CosteoProduccion.objects.filter(
                    orden_fabricacion__fecha_fin_programada__range=(week_start, week_end),
                ).update(fecha_actualizacion=period_timestamp)
                generar_reporte_financiero(
                    usuario=usuario,
                    fecha_inicio=week_start,
                    fecha_fin=week_end,
                )

        self.stdout.write(self.style.SUCCESS(
            f'Datos coherentes regenerados: {periods} periodos, '
            f'{periods * 3} órdenes segmentadas y {periods} reportes financieros.'
        ))
        self.stdout.write('Los reportes financieros se calcularon después de consolidar los costeos de producción.')
