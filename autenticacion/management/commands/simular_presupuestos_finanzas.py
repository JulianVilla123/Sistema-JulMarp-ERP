from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from autenticacion.models import PresupuestoFinanciero


class Command(BaseCommand):
    help = 'Genera presupuestos financieros simulados para validar el dashboard.'

    marker = 'ML_PRESUPUESTOS_FINANZAS_2026'

    def add_arguments(self, parser):
        parser.add_argument(
            '--start-date',
            help='Fecha de inicio en formato AAAA-MM-DD. Por defecto, 29 días antes de hoy.',
        )
        parser.add_argument(
            '--end-date',
            help='Fecha de fin en formato AAAA-MM-DD. Por defecto, hoy.',
        )
        parser.add_argument(
            '--reset',
            action='store_true',
            help='Reemplaza únicamente los presupuestos creados por esta simulación.',
        )

    def handle(self, *args, **options):
        start_date = self.parse_date(options.get('start_date')) or (timezone.localdate() - timedelta(days=29))
        end_date = self.parse_date(options.get('end_date')) or timezone.localdate()
        if start_date > end_date:
            raise CommandError('La fecha de inicio no puede ser posterior a la fecha de fin.')

        User = get_user_model()
        usuario = User.objects.filter(is_superuser=True).order_by('id').first() or User.objects.order_by('id').first()
        if not usuario:
            raise CommandError('Se necesita al menos un usuario ERP para crear los presupuestos simulados.')

        records = [
            ('Ingreso por ventas simuladas', PresupuestoFinanciero.Categoria.INGRESO, Decimal('650000.00'), Decimal('612500.00')),
            ('Gasto de operación simulado', PresupuestoFinanciero.Categoria.GASTO, Decimal('360000.00'), Decimal('388000.00')),
            ('OPEX administrativo simulado', PresupuestoFinanciero.Categoria.OPEX, Decimal('120000.00'), Decimal('110000.00')),
            ('CAPEX de mejora simulado', PresupuestoFinanciero.Categoria.CAPEX, Decimal('90000.00'), Decimal('105000.00')),
        ]

        with transaction.atomic():
            if options['reset']:
                PresupuestoFinanciero.objects.filter(descripcion__startswith=self.marker).delete()
            for name, category, planned, actual in records:
                PresupuestoFinanciero.objects.update_or_create(
                    nombre=f'{self.marker} | {name}',
                    defaults={
                        'categoria': category,
                        'periodicidad': PresupuestoFinanciero.Periodicidad.MENSUAL,
                        'fecha_inicio': start_date,
                        'fecha_fin': end_date,
                        'monto_presupuestado': planned,
                        'monto_real': actual,
                        'descripcion': f'{self.marker} | Datos de demostración para validar el dashboard.',
                        'activo': True,
                        'creado_por': usuario,
                        'actualizado_por': usuario,
                    },
                )

        self.stdout.write(self.style.SUCCESS('Presupuestos simulados creados correctamente.'))
        self.stdout.write(f'Periodo aplicado: {start_date} a {end_date}.')
        self.stdout.write('Registros: ingreso, gasto, OPEX y CAPEX.')

    @staticmethod
    def parse_date(value):
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise CommandError('Las fechas deben usar el formato AAAA-MM-DD.') from exc
