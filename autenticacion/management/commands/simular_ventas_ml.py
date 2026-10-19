from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from autenticacion.models import ClienteCompra, CuentaPorPagarCobrar


class Command(BaseCommand):
    help = 'Genera ventas simuladas por semana para validar el pronóstico de Finanzas.'

    marker = 'ML_VENTAS_CLIENTES_2026'

    def add_arguments(self, parser):
        parser.add_argument(
            '--periods',
            type=int,
            default=12,
            help='Cantidad de semanas históricas a simular (por defecto: 12).',
        )
        parser.add_argument(
            '--start-date',
            help='Lunes de inicio en formato AAAA-MM-DD. Por defecto, usa semanas completas recientes.',
        )
        parser.add_argument(
            '--reset',
            action='store_true',
            help='Reemplaza únicamente los registros creados por esta simulación.',
        )

    def handle(self, *args, **options):
        periods = options['periods']
        if periods < 4:
            raise CommandError('--periods debe ser mayor o igual a 4 para evaluar una tendencia.')

        if options.get('start_date'):
            try:
                start_date = date.fromisoformat(options['start_date'])
            except ValueError as exc:
                raise CommandError('--start-date debe usar el formato AAAA-MM-DD.') from exc
            if start_date.weekday() != 0:
                raise CommandError('--start-date debe corresponder a un lunes.')
        else:
            current_week_start = timezone.localdate() - timedelta(days=timezone.localdate().weekday())
            start_date = current_week_start - timedelta(weeks=periods)

        User = get_user_model()
        usuario = User.objects.filter(is_superuser=True).order_by('id').first()
        if not usuario:
            usuario = User.objects.order_by('id').first()
        if not usuario:
            raise CommandError('Se necesita al menos un usuario ERP para crear los registros simulados.')

        with transaction.atomic():
            if options['reset']:
                CuentaPorPagarCobrar.objects.filter(observaciones__startswith=self.marker).delete()
            clientes = self.ensure_clients()
            created = 0
            for period_index in range(periods):
                week_start = start_date + timedelta(weeks=period_index)
                amounts = self.week_amounts(period_index)
                for client_index, (client, amount) in enumerate(zip(clientes, amounts), start=1):
                    issue_date = week_start + timedelta(days=client_index - 1)
                    folio = f'CXC-ML-{period_index + 1:02d}-{client.codigo}'
                    _, was_created = CuentaPorPagarCobrar.objects.update_or_create(
                        folio=folio,
                        defaults={
                            'tipo': CuentaPorPagarCobrar.TipoCuenta.POR_COBRAR,
                            'tercero_nombre': client.nombre,
                            'cliente_compra': client,
                            'monto_total': amount,
                            'monto_pagado': Decimal('0'),
                            'fecha_emision': issue_date,
                            'fecha_vencimiento': issue_date + timedelta(days=30),
                            'estado': CuentaPorPagarCobrar.EstadoCuenta.PENDIENTE,
                            'observaciones': f'{self.marker} | segmento={client.codigo}',
                            'creado_por': usuario,
                            'actualizado_por': usuario,
                        },
                    )
                    created += int(was_created)

        total = CuentaPorPagarCobrar.objects.filter(observaciones__startswith=self.marker).count()
        self.stdout.write(self.style.SUCCESS(
            f'Simulación de ventas completada: {periods} semanas, {total} cuentas por cobrar '
            f'({created} nuevas).'
        ))
        self.stdout.write(f'Historial: {start_date} a {start_date + timedelta(weeks=periods - 1)}.')
        self.stdout.write('Fuente del pronóstico: monto_total de cuentas POR_COBRAR agrupado por semana.')

    @staticmethod
    def ensure_clients():
        client_data = [
            ('CLI-ML-01', 'Cliente Alfa ML'),
            ('CLI-ML-02', 'Cliente Beta ML'),
            ('CLI-ML-03', 'Cliente Gamma ML'),
        ]
        clients = []
        for code, name in client_data:
            client, _ = ClienteCompra.objects.get_or_create(
                codigo=code,
                defaults={'nombre': name, 'activo': True},
            )
            clients.append(client)
        return clients

    @staticmethod
    def week_amounts(period_index):
        # Las ventas se derivan de la producción simulada: tres segmentos,
        # precio de venta unitario común y distribución por cliente. Así el
        # ingreso financiero puede reconciliarse con las piezas producidas.
        production = [
            (Decimal('620'), Decimal('35'), [0, 12, -8, 15, -5, 10, 6, -10, 18, 8]),
            (Decimal('920'), Decimal('-28'), [0, -10, 8, -12, 5, -8, 10, -6, 4, -10]),
            (Decimal('760'), Decimal('0'), [0, 18, -12, 10, -6, 14, -10, 8, -4, 12]),
        ]
        total_produced = sum(
            base + trend * Decimal(period_index) + Decimal(str(variation[period_index % len(variation)]))
            for base, trend, variation in production
        )
        total_sales = (total_produced * Decimal('12.50')).quantize(Decimal('0.01'))
        return [
            (total_sales * Decimal('0.40')).quantize(Decimal('0.01')),
            (total_sales * Decimal('0.35')).quantize(Decimal('0.01')),
            total_sales - (total_sales * Decimal('0.40')).quantize(Decimal('0.01')) - (total_sales * Decimal('0.35')).quantize(Decimal('0.01')),
        ]
