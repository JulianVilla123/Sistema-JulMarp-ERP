import json
from decimal import Decimal

from django.core.management.base import BaseCommand

from autenticacion.ml_analytics import (
    calcular_prediccion_ventas,
    calcular_predicciones_calidad,
    calcular_predicciones_financieras,
    calcular_predicciones_inventario,
    calcular_predicciones_mantenimiento,
    calcular_predicciones_mfg,
)


def _decimal(value):
    return Decimal(str(value or 0))


def _comparar(nombre, base, enriquecido, base_value, enriched_value):
    base_value = _decimal(base_value)
    enriched_value = _decimal(enriched_value)
    diferencia = enriched_value - base_value
    porcentaje = (diferencia / abs(base_value) * Decimal('100')) if base_value else None
    return {
        'departamento': nombre,
        'base_historica': float(base_value),
        'modelo_enriquecido': float(enriched_value),
        'diferencia': float(diferencia),
        'diferencia_pct': float(porcentaje) if porcentaje is not None else None,
        'factores_aplicados': bool(enriquecido.get('external_factors_applied')),
        'factores': enriquecido.get('external_factors', []),
        'muestras': enriquecido.get('samples', 0),
    }


class Command(BaseCommand):
    help = 'Compara la línea base histórica contra el pronóstico enriquecido con factores externos.'

    def add_arguments(self, parser):
        parser.add_argument('--json', action='store_true', help='Devuelve el resultado en JSON.')

    def handle(self, *args, **options):
        ventas_base = calcular_prediccion_ventas(usar_factores_externos=False)
        ventas_enriquecido = calcular_prediccion_ventas(usar_factores_externos=True)
        finanzas_base = calcular_predicciones_financieras(usar_factores_externos=False)
        finanzas_enriquecido = calcular_predicciones_financieras(usar_factores_externos=True)
        inventario_base = calcular_predicciones_inventario(horizon_days=28, usar_factores_externos=False)
        inventario_enriquecido = calcular_predicciones_inventario(horizon_days=28, usar_factores_externos=True)
        mfg_base = calcular_predicciones_mfg(horizon_weeks=4, usar_factores_externos=False)
        mfg_enriquecido = calcular_predicciones_mfg(horizon_weeks=4, usar_factores_externos=True)
        qa_base = calcular_predicciones_calidad(horizon_weeks=4, usar_factores_externos=False)
        qa_enriquecido = calcular_predicciones_calidad(horizon_weeks=4, usar_factores_externos=True)
        mantenimiento_base = calcular_predicciones_mantenimiento(horizon_weeks=4, usar_factores_externos=False)
        mantenimiento_enriquecido = calcular_predicciones_mantenimiento(horizon_weeks=4, usar_factores_externos=True)

        finanzas_base_item = next((item for item in finanzas_base['predictions'] if item['key'] == 'ventas_clientes'), {})
        finanzas_enriquecido_item = next((item for item in finanzas_enriquecido['predictions'] if item['key'] == 'ventas_clientes'), {})
        mfg_base_item = next((item for item in mfg_base['predictions'] if item['code'] == 'unidades_totales'), {})
        mfg_enriquecido_item = next((item for item in mfg_enriquecido['predictions'] if item['code'] == 'unidades_totales'), {})

        resultado = {
            'ventas': _comparar('Finanzas · ventas', ventas_base, ventas_enriquecido, ventas_base.get('next_week'), ventas_enriquecido.get('next_week')),
            'finanzas': _comparar('Finanzas · KPI', finanzas_base, finanzas_enriquecido, finanzas_base_item.get('value'), finanzas_enriquecido_item.get('value')),
            'inventario': _comparar(
                'Inventario · punto de reorden',
                inventario_base,
                inventario_enriquecido,
                inventario_base.get('summary', {}).get('punto_reorden_total'),
                inventario_enriquecido.get('summary', {}).get('punto_reorden_total'),
            ),
            'mfg': _comparar('MFG · producción', mfg_base, mfg_enriquecido, mfg_base_item.get('value'), mfg_enriquecido_item.get('value')),
            'qa': _comparar('QA · tasa de defecto', qa_base, qa_enriquecido, qa_base.get('trend_forecast'), qa_enriquecido.get('trend_forecast')),
            'mantenimiento': _comparar('Mantenimiento · paros', mantenimiento_base, mantenimiento_enriquecido, mantenimiento_base.get('trend_forecast'), mantenimiento_enriquecido.get('trend_forecast')),
        }
        resultado['metodo_base'] = 'Regresión lineal semanal sobre datos históricos'
        resultado['metodo_enriquecido'] = 'Regresión lineal semanal + variables externas operativas'
        resultado['criterio_comparacion'] = 'Se compara el mismo horizonte con y sin factores; una diferencia distinta de cero confirma que las variables externas entraron al cálculo.'
        resultado['inventario']['señales_operativas'] = {
            'ordenes_retrasadas': inventario_enriquecido.get('summary', {}).get('ordenes_retrasadas', 0),
            'costo_sugerido_compra': float(inventario_enriquecido.get('summary', {}).get('costo_sugerido_compra') or 0),
        }
        comparaciones = sum(
            1 for item in resultado.values()
            if isinstance(item, dict) and 'factores_aplicados' in item
        )
        resultado['factores_conectados'] = sum(
            item['factores_aplicados'] for item in resultado.values()
            if isinstance(item, dict) and 'factores_aplicados' in item
        )
        resultado['comparaciones'] = comparaciones
        resultado['validacion'] = 'OK' if resultado['factores_conectados'] == resultado['comparaciones'] else 'PENDIENTE'

        if options['json']:
            self.stdout.write(json.dumps(resultado, ensure_ascii=False))
            return
        self.stdout.write(f"Validación de modelo enriquecido: {resultado['validacion']}")
        for item in resultado.values():
            if not isinstance(item, dict):
                continue
            self.stdout.write(
                f"[{item['departamento']}] base={item['base_historica']:.2f} | "
                f"enriquecido={item['modelo_enriquecido']:.2f} | "
                f"diferencia={item['diferencia']:.2f} | "
                f"factores={'sí' if item['factores_aplicados'] else 'no'}"
            )
