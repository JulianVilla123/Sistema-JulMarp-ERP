from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.template import Context, Template
from django.test import TestCase
from django.utils import timezone

from .finanzas import calcular_dashboard_finanzas
from .ml_analytics import calcular_prediccion_ventas
from .models import ClienteCompra, CuentaPorPagarCobrar, OrdenCompra, PresupuestoFinanciero, Proveedor


class SalesForecastTests(TestCase):
    def test_forecast_uses_weekly_accounts_receivable(self):
        User = get_user_model()
        user = User.objects.create_user(
            username='sales-forecast-test',
            password='test-password',
            numero_empleado='TEST-SALES-001',
        )
        client = ClienteCompra.objects.create(
            codigo='TEST-CLIENT-001',
            nombre='Cliente de prueba ventas',
        )
        current_week = timezone.localdate() - timedelta(days=timezone.localdate().weekday())
        for index in range(8):
            issue_date = current_week - timedelta(weeks=8 - index) + timedelta(days=1)
            CuentaPorPagarCobrar.objects.create(
                tipo=CuentaPorPagarCobrar.TipoCuenta.POR_COBRAR,
                folio=f'TEST-CXC-{index:02d}',
                tercero_nombre=client.nombre,
                cliente_compra=client,
                monto_total=Decimal('1000') + Decimal(index * 100),
                fecha_emision=issue_date,
                fecha_vencimiento=issue_date + timedelta(days=30),
                estado=CuentaPorPagarCobrar.EstadoCuenta.PENDIENTE,
                creado_por=user,
                actualizado_por=user,
            )

        forecast = calcular_prediccion_ventas(limit=12, horizon_weeks=4)

        self.assertTrue(forecast['enabled'])
        self.assertEqual(forecast['samples'], 8)
        self.assertEqual(forecast['transactions'], 8)
        self.assertGreater(forecast['next_week'], forecast['current_value'])
        self.assertEqual(len(forecast['forecast_values']), 4)
        self.assertEqual(len(forecast['chart']['labels']), 12)


class NumberFormattingTests(TestCase):
    def test_display_numbers_use_thousands_separator(self):
        template = Template('{{ value|floatformat:2 }}|{{ value|floatformat:0 }}|{{ value|floatformat:\'0u\' }}')

        rendered = template.render(Context({'value': Decimal('464000.00')}))

        self.assertEqual(rendered, '464,000.00|464,000|464000')


class FinanceBudgetDashboardTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='budget-dashboard-test',
            password='test-password',
            numero_empleado='TEST-BUDGET-001',
        )
        self.today = timezone.localdate()
        PresupuestoFinanciero.objects.create(
            nombre='Presupuesto de prueba',
            categoria=PresupuestoFinanciero.Categoria.INGRESO,
            periodicidad=PresupuestoFinanciero.Periodicidad.MENSUAL,
            fecha_inicio=self.today - timedelta(days=29),
            fecha_fin=self.today,
            monto_presupuestado=Decimal('5000'),
            monto_real=Decimal('4200'),
            activo=True,
            creado_por=self.user,
            actualizado_por=self.user,
        )

    def test_dashboard_includes_budget_that_overlaps_selected_dates(self):
        dashboard = calcular_dashboard_finanzas(
            fecha_inicio=self.today - timedelta(days=10),
            fecha_fin=self.today,
        )

        self.assertTrue(dashboard['charts']['presupuesto_has_data'])
        self.assertEqual(dashboard['charts']['presupuesto_count'], 1)
        self.assertEqual(dashboard['charts']['presupuesto_values'][2], 5000.0)

    def test_dashboard_excludes_budget_outside_selected_dates(self):
        dashboard = calcular_dashboard_finanzas(
            fecha_inicio=self.today - timedelta(days=90),
            fecha_fin=self.today - timedelta(days=60),
        )

        self.assertFalse(dashboard['charts']['presupuesto_has_data'])
        self.assertEqual(dashboard['charts']['presupuesto_count'], 0)
        self.assertEqual(dashboard['charts']['presupuesto_values'], [0.0, 0.0, 0.0])

    def test_dashboard_separates_costs_from_profitability(self):
        dashboard = calcular_dashboard_finanzas(
            fecha_inicio=self.today - timedelta(days=10),
            fecha_fin=self.today,
        )

        self.assertEqual(dashboard['charts']['costos_labels'], ['Costo planificado', 'Costo real'])
        self.assertEqual(len(dashboard['charts']['costos_values']), 2)
        self.assertIn('rentabilidad_has_data', dashboard['produccion'])
        self.assertFalse(dashboard['produccion']['rentabilidad_has_data'])


class PurchaseOrderChartTests(TestCase):
    def test_dashboard_marks_purchase_chart_when_order_is_in_period(self):
        User = get_user_model()
        user = User.objects.create_user(
            username='purchase-chart-test',
            password='test-password',
            numero_empleado='TEST-ORDER-001',
        )
        supplier = Proveedor.objects.create(nombre='Proveedor de prueba órdenes')
        today = timezone.localdate()
        OrdenCompra.objects.create(
            folio='TEST-OC-001',
            proveedor=supplier,
            fecha_orden=today - timedelta(days=2),
            estado=OrdenCompra.EstadoOrden.ENVIADA,
            creado_por=user,
        )

        dashboard = calcular_dashboard_finanzas(
            fecha_inicio=today - timedelta(days=10),
            fecha_fin=today,
        )

        self.assertTrue(dashboard['charts']['oc_has_data'])
        self.assertEqual(dashboard['charts']['oc_values'], [1, 0])
