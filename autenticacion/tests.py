from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.template import Context, Template
from django.test import TestCase
from django.utils import timezone

from .finanzas import calcular_dashboard_finanzas
from .ml_analytics import calcular_prediccion_ventas, calcular_predicciones_mfg
from .models import (
    ClienteCompra,
    CuentaPorPagarCobrar,
    OrdenCompra,
    PresupuestoFinanciero,
    Proveedor,
    ReporteKPIProduccion,
)
from .predictive_data_quality import auditar_datos_predictivos
from .predictive_factors import variables_calendario
from .predictive_contract import HORIZONTES_SEMANAS, horizonte_a_dias, normalizar_horizonte_semanas
from .predictive_regression import (
    ajustar_regresion_multiple,
    evaluar_regresion_multiple_temporal,
    predecir_regresion_multiple,
    validar_datos_regresion_multiple,
)


class PredictiveContractTests(TestCase):
    def test_all_departments_use_the_same_weekly_horizons(self):
        self.assertEqual(HORIZONTES_SEMANAS, (1, 4, 8, 12))
        self.assertEqual(normalizar_horizonte_semanas('8'), 8)
        self.assertEqual(normalizar_horizonte_semanas('30'), 4)
        self.assertEqual(horizonte_a_dias(12), 84)


class PredictiveDataQualityTests(TestCase):
    def test_audit_returns_one_dataset_per_predictive_department(self):
        result = auditar_datos_predictivos(days=42)

        self.assertGreaterEqual(result['semanas_esperadas'], 4)
        self.assertEqual(
            [dataset['departamento'] for dataset in result['datasets']],
            ['Inventario', 'MFG', 'Finanzas', 'QA', 'Mantenimiento'],
        )
        self.assertEqual(result['criticos'], 5)


class PredictiveFactorsTests(TestCase):
    def test_calendar_variables_are_shared_and_deterministic(self):
        variables = variables_calendario(timezone.datetime(2026, 10, 31).date())

        self.assertEqual(variables['mes'], 10)
        self.assertEqual(variables['trimestre'], 4)
        self.assertEqual(variables['fin_de_mes'], 1)


class MultipleRegressionTests(TestCase):
    def test_multiple_regression_learns_two_predictors(self):
        features = [[1, 2], [2, 1], [3, 4], [4, 3], [5, 6], [6, 5]]
        targets = [6, 7.5, 11, 12.5, 16, 17.5]

        model = ajustar_regresion_multiple(
            features,
            targets,
            ['demanda', 'paros'],
            ridge_alpha=0,
        )

        self.assertTrue(model['enabled'])
        self.assertEqual(model['features'], ['demanda', 'paros'])
        self.assertAlmostEqual(float(model['intercepto']), 3.0, places=6)
        self.assertAlmostEqual(float(model['coeficientes']['demanda']), 2.0, places=6)
        self.assertAlmostEqual(float(model['coeficientes']['paros']), 0.5, places=6)
        self.assertAlmostEqual(float(model['r2']), 1.0, places=6)
        self.assertAlmostEqual(float(predecir_regresion_multiple(model, [[7, 8]])[0]), 21.0, places=6)

    def test_multiple_regression_rejects_insufficient_observations(self):
        model = ajustar_regresion_multiple([[1, 2], [2, 1]], [3, 4], ['demanda', 'paros'])

        self.assertFalse(model['enabled'])
        self.assertIn('observaciones', model['reason'])

    def test_multiple_regression_validates_shape_and_non_finite_values(self):
        shape = validar_datos_regresion_multiple([[1, 2], [2]], [3, 4], ['demanda', 'paros'])
        non_finite = validar_datos_regresion_multiple([[1, 'NaN'], [2, 3]], [3, 4], ['demanda', 'paros'])

        self.assertFalse(shape['valid'])
        self.assertTrue(any('fila 2' in issue.lower() for issue in shape['issues']))
        self.assertFalse(non_finite['valid'])
        self.assertTrue(any('no finito' in issue for issue in non_finite['issues']))

    def test_multiple_regression_uses_forward_temporal_validation(self):
        features = [[index, index * index] for index in range(1, 9)]
        targets = [Decimal(index * 2 + 3) for index in range(1, 9)]

        validation = evaluar_regresion_multiple_temporal(
            features,
            targets,
            ['semana', 'demanda'],
            min_samples=4,
            ridge_alpha=0,
        )

        self.assertEqual(validation['backtests'], 4)
        self.assertIsNotNone(validation['mape'])
        self.assertIn('completada', validation['reason'])

    def test_mfg_exposes_multiple_regression_comparison_without_replacing_baseline(self):
        current_week = timezone.localdate() - timedelta(days=timezone.localdate().weekday())
        for index in range(10):
            end_date = current_week - timedelta(weeks=9 - index) + timedelta(days=4)
            ReporteKPIProduccion.objects.create(
                fecha_inicio=end_date - timedelta(days=4),
                fecha_fin=end_date,
                disponibilidad=Decimal('80') + index,
                rendimiento=Decimal('82') + index,
                calidad=Decimal('90') + Decimal(index) / Decimal('2'),
                oee=Decimal('60') + index,
                tiempo_ciclo_promedio=Decimal('10') - Decimal(index) / Decimal('10'),
                tasa_rechazo=Decimal('5') - Decimal(index) / Decimal('10'),
                cumplimiento_ordenes=Decimal('75') + index,
                utilizacion_recursos=Decimal('70') + index,
                detalle={
                    'total_ordenes': 20 + index,
                    'horas_programadas': 120 + index,
                    'horas_reales': 130 + index,
                    'unidades_totales': 1000 + (index * 20),
                },
            )

        forecast = calcular_predicciones_mfg(horizon_weeks=4)

        comparison = next(item for item in forecast['multiple_comparison'] if item['code'] == 'oee')
        self.assertTrue(forecast['multiple_model_available'])
        self.assertTrue(comparison['enabled'])
        self.assertGreaterEqual(comparison['backtests'], 1)
        self.assertEqual(forecast['model_variant'], 'Tendencia lineal')


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
