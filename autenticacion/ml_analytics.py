from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Q, Sum
from django.utils import timezone

from .models import (
    InventarioAlmacen,
    OrdenFabricacion,
    RecepcionMaterialDetalle,
    ReporteFinanciero,
    ReporteKPIProduccion,
    RegistroScrapDefecto,
    RegistroUsoRecursoProduccion,
    SalidaLineaDetalle,
)

ZERO = Decimal('0')
ONE = Decimal('1')


def _to_decimal(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value in (None, ''):
        return ZERO
    return Decimal(str(value))


def _round(value: Decimal, places: str = '0.01') -> Decimal:
    return _to_decimal(value).quantize(Decimal(places), rounding=ROUND_HALF_UP)


def _safe_div(numerator: Decimal, denominator: Decimal) -> Decimal:
    denominator = _to_decimal(denominator)
    if denominator <= 0:
        return ZERO
    return _to_decimal(numerator) / denominator


def _bounded_percent(value: Decimal) -> Decimal:
    return max(ZERO, min(Decimal('100'), _to_decimal(value)))


def _float_or_none(value):
    if value is None:
        return None
    return float(_round(_to_decimal(value)))


def _weighted_average(values: list[Decimal]) -> Decimal:
    if not values:
        return ZERO
    weight_total = ZERO
    weighted_total = ZERO
    for index, value in enumerate(values, start=1):
        weight = Decimal(index)
        weighted_total += _to_decimal(value) * weight
        weight_total += weight
    return _safe_div(weighted_total, weight_total)


def _linear_forecast(
    values: list[Decimal],
    steps: int = 1,
    x_values: list[Decimal] | None = None,
    future_x_values: list[Decimal] | None = None,
) -> tuple[list[Decimal], Decimal, str]:
    steps = max(1, int(steps or 1))
    clean_values = [_to_decimal(value) for value in values]
    if not clean_values:
        return [], ZERO, 'Sin datos historicos suficientes.'
    if len(clean_values) == 1:
        return [clean_values[0] for _ in range(steps)], ZERO, 'Prediccion basada en un solo punto historico.'

    n = Decimal(len(clean_values))
    xs = x_values or [Decimal(index) for index in range(len(clean_values))]
    xs = [_to_decimal(value) for value in xs]
    if len(xs) != len(clean_values):
        xs = [Decimal(index) for index in range(len(clean_values))]
    mean_x = sum(xs, ZERO) / n
    mean_y = sum(clean_values, ZERO) / n
    numerator = sum(((x - mean_x) * (y - mean_y) for x, y in zip(xs, clean_values)), ZERO)
    denominator = sum(((x - mean_x) ** 2 for x in xs), ZERO)
    slope = _safe_div(numerator, denominator)
    intercept = mean_y - (slope * mean_x)
    target_xs = future_x_values or [xs[-1] + Decimal(offset) for offset in range(1, steps + 1)]
    predictions = [intercept + (slope * _to_decimal(x_value)) for x_value in target_xs]

    residuals = [abs(y - (intercept + slope * x)) for x, y in zip(xs, clean_values)]
    mean_error = _safe_div(sum(residuals, ZERO), Decimal(len(residuals)))
    baseline = abs(mean_y) if mean_y else ONE
    confidence = max(Decimal('35'), min(Decimal('95'), Decimal('100') - (_safe_div(mean_error, baseline) * Decimal('100'))))
    detail = f"Regresion lineal con {len(clean_values)} periodos historicos."
    return predictions, confidence, detail

def _status_for_metric(code: str, value: Decimal) -> str:
    value = _to_decimal(value)
    if code in {'oee', 'cumplimiento_ordenes', 'utilizacion_recursos'}:
        if value >= Decimal('85'):
            return 'verde'
        if value >= Decimal('70'):
            return 'amarillo'
        return 'rojo'
    if code in {'tasa_rechazo', 'variacion_costos_pct'}:
        abs_value = abs(value)
        if abs_value <= Decimal('5'):
            return 'verde'
        if abs_value <= Decimal('10'):
            return 'amarillo'
        return 'rojo'
    return 'amarillo'


def _evaluate_forecast(values: list[Decimal], horizon_weeks: int, x_values: list[Decimal] | None = None) -> dict:
    errors = []
    baseline_errors = []
    percentage_errors = []
    for index in range(3, len(values)):
        predicted, _, _ = _linear_forecast(
            values[:index],
            steps=1,
            x_values=x_values[:index] if x_values else None,
            future_x_values=[x_values[index]] if x_values else None,
        )
        actual = values[index]
        if not predicted:
            continue
        error = abs(actual - predicted[0])
        errors.append(error)
        baseline_errors.append(abs(actual - values[index - 1]))
        if actual:
            percentage_errors.append(error / abs(actual) * Decimal('100'))

    mae = _safe_div(sum(errors, ZERO), Decimal(len(errors))) if errors else ZERO
    mape = _safe_div(sum(percentage_errors, ZERO), Decimal(len(percentage_errors))) if percentage_errors else None
    baseline_mae = _safe_div(sum(baseline_errors, ZERO), Decimal(len(baseline_errors))) if baseline_errors else ZERO
    skill = None if not errors or baseline_mae == 0 else (ONE - mae / baseline_mae) * Decimal('100')
    sample_factor = min(ONE, Decimal(len(values)) / Decimal('8'))
    error_factor = max(ZERO, ONE - _safe_div(mae, max(abs(_weighted_average(values)), ONE))) if errors else Decimal('0.35')
    horizon_factor = ONE / (ONE + Decimal(max(horizon_weeks - 1, 0)) / Decimal('8'))
    confidence = Decimal('100') * sample_factor * error_factor * horizon_factor
    return {
        'mae': _round(mae),
        'mape': _round(mape, '0.1') if mape is not None else None,
        'skill': _round(skill, '0.1') if skill is not None else None,
        'backtests': len(errors),
        'confidence': _round(confidence, '0.1'),
    }


def calcular_predicciones_mfg(limit: int = 104, horizon_weeks: int = 1) -> dict:
    horizon_weeks = max(1, min(int(horizon_weeks or 1), 12))
    reports_by_week = {}
    for report in ReporteKPIProduccion.objects.order_by('-fecha_fin', '-fecha_generacion'):
        week_start = report.fecha_fin - timedelta(days=report.fecha_fin.weekday())
        if week_start not in reports_by_week:
            reports_by_week[week_start] = report
        if len(reports_by_week) >= limit:
            break
    weekly_reports = sorted(reports_by_week.items(), key=lambda item: item[0])
    week_starts = [week for week, _ in weekly_reports]
    reports = [report for _, report in weekly_reports]
    report_dates = [report.fecha_fin for report in reports]
    x_values = [Decimal((report_date - report_dates[0]).days) / Decimal('7') for report_date in report_dates] if report_dates else []
    labels = [report_date.strftime('%d/%m/%y') for report_date in report_dates]
    forecast_labels = [
        (report_dates[-1] + timedelta(weeks=week)).strftime('%d/%m/%y')
        for week in range(1, horizon_weeks + 1)
    ] if report_dates else [f'Semana +{week}' for week in range(1, horizon_weeks + 1)]
    future_x_values = [x_values[-1] + Decimal(week) for week in range(1, horizon_weeks + 1)] if x_values else []

    metric_config = [
        ('oee', 'Eficiencia global (OEE)', '%', 'oee', True),
        ('disponibilidad', 'Disponibilidad', '%', 'oee', True),
        ('rendimiento', 'Rendimiento', '%', 'oee', True),
        ('calidad', 'Calidad', '%', 'oee', True),
        ('unidades_totales', 'Produccion total', ' piezas', 'produccion', True),
        ('tiempo_ciclo_promedio', 'Tiempo de ciclo', ' min/ud', 'tiempo_ciclo', False),
        ('tasa_rechazo', 'Tasa de rechazo', '%', 'tasa_rechazo', False),
        ('cumplimiento_ordenes', 'Cumplimiento de órdenes', '%', 'cumplimiento_ordenes', True),
        ('variacion_costos', 'Variación de costos', ' $', 'variacion_costos_pct', False),
        ('costo_real', 'Costo real', ' $', 'costo_real', False),
        ('costo_planificado', 'Costo planificado', ' $', 'costo_planificado', False),
        ('utilizacion_recursos', 'Uso de recursos', '%', 'utilizacion_recursos', False),
    ]

    predictions = []
    trend_charts = []
    action_by_metric = {
        'oee': 'Revisar paros, disponibilidad de maquinas y causas de perdida de rendimiento.',
        'disponibilidad': 'Revisar paros y mantenimiento de los equipos con mayor tiempo detenido.',
        'rendimiento': 'Comparar el tiempo real de ciclo con el estandar y revisar cambios de turno.',
        'calidad': 'Revisar defectos recurrentes y confirmar acciones correctivas con Calidad.',
        'unidades_totales': 'Comparar el volumen proyectado con pedidos, inventario y capacidad disponible.',
        'tiempo_ciclo_promedio': 'Validar tiempos de preparacion, esperas y operaciones con mayor duracion.',
        'tasa_rechazo': 'Identificar las causas de scrap mas frecuentes y revisar las ordenes afectadas.',
        'cumplimiento_ordenes': 'Priorizar ordenes atrasadas y confirmar materiales y capacidad disponible.',
        'variacion_costos': 'Comparar materiales y horas de recurso contra el costo presupuestado.',
        'costo_real': 'Revisar las ordenes y recursos que concentran el mayor costo real.',
        'costo_planificado': 'Confirmar que los reportes KPI incluyan costos planeados completos.',
        'utilizacion_recursos': 'Comparar horas registradas contra capacidad y programa de produccion.',
    }

    def metric_value(report, field_name):
        if field_name == 'unidades_totales':
            return _to_decimal((report.detalle or {}).get('unidades_totales'))
        return _to_decimal(getattr(report, field_name))

    for field_name, label, unit, status_code, higher_is_better in metric_config:
        values = [metric_value(report, field_name) for report in reports]
        forecast_values, _, _ = _linear_forecast(values, steps=horizon_weeks, x_values=x_values, future_x_values=future_x_values)
        if len(values) < 3:
            fallback = _weighted_average(values)
            forecast_values = [fallback for _ in range(horizon_weeks)] if values else []
        bounded = field_name in {'oee', 'disponibilidad', 'rendimiento', 'calidad', 'tasa_rechazo', 'cumplimiento_ordenes', 'utilizacion_recursos'}
        nonnegative = bounded or field_name == 'unidades_totales'
        if bounded:
            forecast_values = [_bounded_percent(value) for value in forecast_values]
        evaluation = _evaluate_forecast(values, horizon_weeks, x_values=x_values)
        forecast = forecast_values[-1] if forecast_values else None
        residual_scale = evaluation['mae'] if evaluation['backtests'] else ZERO
        if not residual_scale and len(values) > 1:
            residual_scale = _safe_div(sum((abs(values[i] - values[i - 1]) for i in range(1, len(values))), ZERO), Decimal(len(values) - 1))
        margin = residual_scale * Decimal(str(horizon_weeks ** 0.5))
        lower = (max(ZERO, forecast - margin) if nonnegative else forecast - margin) if forecast is not None else None
        upper = forecast + margin if forecast is not None else None
        if bounded and upper is not None:
            upper = min(Decimal('100'), upper)

        if forecast is None:
            status = 'amarillo'
        elif field_name in {'variacion_costos', 'costo_real', 'costo_planificado', 'unidades_totales'}:
            status = 'amarillo'
        elif field_name == 'tiempo_ciclo_promedio':
            status = 'verde' if len(values) > 1 and forecast <= values[-1] else ('rojo' if forecast > (values[-1] * Decimal('1.10')) else 'amarillo')
        elif field_name == 'utilizacion_recursos':
            status = 'verde' if Decimal('70') <= forecast <= Decimal('95') else ('rojo' if forecast > Decimal('98') or forecast < Decimal('60') else 'amarillo')
        elif higher_is_better:
            status = _status_for_metric(status_code, forecast)
        else:
            status = 'verde' if forecast <= Decimal('5') else ('amarillo' if forecast <= Decimal('10') else 'rojo')

        detail = 'Se necesitan al menos 3 periodos para estimar tendencia.' if len(values) < 3 else 'Tendencia lineal; contrastada con periodos historicos.'
        predictions.append({
            'code': field_name,
            'label': label,
            'value': _round(forecast) if forecast is not None else None,
            'lower': _round(lower) if lower is not None else None,
            'upper': _round(upper) if upper is not None else None,
            'unit': unit,
            'confidence': evaluation['confidence'],
            'mae': evaluation['mae'],
            'mape': evaluation['mape'],
            'skill': evaluation['skill'],
            'backtests': evaluation['backtests'],
            'status': status,
            'detail': detail,
            'action': action_by_metric[field_name],
            'enough_data': len(values) >= 3,
            'has_value': forecast is not None,
            'has_skill': evaluation['skill'] is not None,
            'has_mape': evaluation['mape'] is not None,
        })

        forecast_series = [None for _ in values]
        if values and forecast_values:
            forecast_series[-1] = _float_or_none(values[-1])
        forecast_series.extend([_float_or_none(value) for value in forecast_values])
        trend_charts.append({
            'code': field_name,
            'label': label,
            'unit': unit,
            'labels': labels + forecast_labels[:len(forecast_values)],
            'history': [_float_or_none(value) for value in values] + [None for _ in forecast_values],
            'forecast': forecast_series,
            'lower': [None for _ in values] + [_float_or_none(max(ZERO, value - residual_scale * Decimal(str(week ** 0.5))) if nonnegative else value - residual_scale * Decimal(str(week ** 0.5))) for week, value in enumerate(forecast_values, start=1)],
            'upper': [None for _ in values] + [_float_or_none(min(Decimal('100'), value + residual_scale * Decimal(str(week ** 0.5))) if bounded else value + residual_scale * Decimal(str(week ** 0.5))) for week, value in enumerate(forecast_values, start=1)],
            'confidence': float(evaluation['confidence']),
            'status': status,
            'bounded': bounded,
        })

    alerts = []
    improving = []
    for item in predictions:
        if item['value'] is None:
            continue
        if item['status'] == 'rojo':
            alerts.append(f"Atencion: {item['label']} podria llegar a {item['value']}{item['unit']} en {horizon_weeks} semanas.")
        elif len(reports) > 1:
            start_value = metric_value(reports[0], item['code'])
            end_value = metric_value(reports[-1], item['code'])
            delta = end_value - start_value
            if abs(delta) >= max(abs(start_value) * Decimal('0.10'), Decimal('1')):
                direction = 'subio' if delta > 0 else 'bajo'
                improving.append(f"{item['label']} {direction} {abs(_round(delta))}{item['unit']} entre el primer y ultimo periodo.")

    if len(reports) < 3:
        data_quality = 'Historial insuficiente: se necesitan al menos 3 semanas con reportes; 8 o mas mejoran la estabilidad.'
    elif len(reports) < 8:
        data_quality = f"Historial limitado: {len(reports)} semanas con reportes. La proyeccion es orientativa."
    else:
        data_quality = f"Historial de {len(reports)} semanas con reportes disponible."
    explanation = alerts[:3] or improving[:3]
    if not explanation:
        explanation = ['No se detectan cambios relevantes con los datos disponibles. Revisa la precision y el rango de incertidumbre antes de planear.']

    return {
        'enabled': bool(reports),
        'samples': len(reports),
        'method': 'Tendencia lineal por fecha de cierre con proyeccion semanal y rango de incertidumbre; hasta 104 semanas',
        'horizon_weeks': horizon_weeks,
        'predictions': predictions,
        'trend_charts': trend_charts,
        'alerts': alerts,
        'explanation': explanation,
        'data_quality': data_quality,
        'backtest_periods': max(0, len(reports) - 3),
    }

def preparar_dataset_ml_produccion(fecha_inicio=None, fecha_fin=None, limit: int = 20) -> dict:
    end_date = fecha_fin or timezone.localdate()
    start_date = fecha_inicio or (end_date - timedelta(days=29))
    if start_date > end_date:
        start_date, end_date = end_date, start_date

    orders = list(
        OrdenFabricacion.objects
        .filter(
            Q(fecha_creacion__date__range=(start_date, end_date)) |
            Q(fecha_actualizacion__date__range=(start_date, end_date)) |
            Q(fecha_fin_real__date__range=(start_date, end_date))
        )
        .select_related('bom')
        .prefetch_related('detalles__material', 'scraps_defectos', 'usos_recursos')
        .distinct()
        .order_by('-fecha_fin_real', '-fecha_actualizacion')[:limit]
    )

    material_ids = {
        detail.material_id
        for order in orders
        for detail in order.detalles.all()
        if detail.material_id
    }
    sku_set = {
        detail.material.sku
        for order in orders
        for detail in order.detalles.all()
        if detail.material_id
    }
    entradas_por_material = {
        row['material_id']: _to_decimal(row['entrada'])
        for row in RecepcionMaterialDetalle.objects
        .filter(
            recepcion__fecha_recepcion__range=(start_date, end_date),
            material_id__in=material_ids,
            estatus=RecepcionMaterialDetalle.EstatusDetalle.ACEPTADO,
        )
        .values('material_id')
        .annotate(entrada=Sum('cantidad_recibida'))
    }
    salidas_por_sku = {
        row['sku']: _to_decimal(row['consumo'])
        for row in SalidaLineaDetalle.objects
        .filter(salida__fecha_salida__range=(start_date, end_date), sku__in=sku_set)
        .values('sku')
        .annotate(consumo=Sum('cantidad_enviada'))
    }

    rows = []
    total_produced = ZERO
    total_scrap = ZERO
    total_input = ZERO
    total_consumed = ZERO
    total_real_cost = ZERO

    for order in orders:
        scraps = list(order.scraps_defectos.all())
        usages = list(order.usos_recursos.all())
        details = list(order.detalles.all())
        produced = _to_decimal(order.cantidad_producida)
        scrap_total = sum((_to_decimal(scrap.cantidad_defectos) for scrap in scraps), ZERO)
        defect_counts = {}
        for scrap in scraps:
            defect_counts[scrap.causa] = _round(defect_counts.get(scrap.causa, ZERO) + _to_decimal(scrap.cantidad_defectos))

        material_input = sum((entradas_por_material.get(detail.material_id, ZERO) for detail in details), ZERO)
        material_consumed = sum((_to_decimal(detail.cantidad_consumida) for detail in details), ZERO)
        line_consumed = sum((salidas_por_sku.get(detail.material.sku, ZERO) for detail in details if detail.material_id), ZERO)
        if line_consumed > 0:
            material_consumed = line_consumed

        machine_hours = sum((_to_decimal(usage.horas_reales) for usage in usages if usage.tipo_recurso == RegistroUsoRecursoProduccion.TipoRecurso.MAQUINA), ZERO)
        operator_hours = sum((_to_decimal(usage.horas_reales) for usage in usages if usage.tipo_recurso == RegistroUsoRecursoProduccion.TipoRecurso.OPERADOR), ZERO)
        real_cost = sum((_to_decimal(usage.costo_total) for usage in usages), ZERO)
        scrap_rate = _safe_div(scrap_total, produced) * Decimal('100') if produced > 0 else ZERO

        total_produced += produced
        total_scrap += scrap_total
        total_input += material_input
        total_consumed += material_consumed
        total_real_cost += real_cost

        rows.append({
            'folio': order.folio,
            'producto': order.bom.producto,
            'piezas_producidas': _round(produced),
            'scrap': _round(scrap_total),
            'scrap_pct': _round(scrap_rate),
            'tipos_defecto': defect_counts,
            'entrada_materiales': _round(material_input),
            'consumo_materiales': _round(material_consumed),
            'diferencia_materiales': _round(material_input - material_consumed),
            'horas_maquina': _round(machine_hours),
            'horas_operador': _round(operator_hours),
            'costo_real': _round(real_cost),
            'costo_planificado': None,
            'scrap_observado': _round(scrap_total),
            'costo_observado': _round(real_cost),
        })

    summary = {
        'piezas_producidas': _round(total_produced),
        'scrap': _round(total_scrap),
        'scrap_pct': _round(_safe_div(total_scrap, total_produced) * Decimal('100') if total_produced > 0 else ZERO),
        'entrada_materiales': _round(total_input),
        'consumo_materiales': _round(total_consumed),
        'diferencia_materiales': _round(total_input - total_consumed),
        'costo_real': _round(total_real_cost),
        'costo_planificado': None,
        'variacion_costos': None,
    }

    return {
        'enabled': bool(rows),
        'fecha_inicio': start_date,
        'fecha_fin': end_date,
        'features': [
            'piezas_producidas',
            'scrap',
            'tipo_defecto',
            'consumo_materiales',
            'costo_hora',
            'horas_maquina',
            'horas_operador',
        ],
        'observed_outputs': ['scrap_observado', 'costo_observado'],
        'summary': summary,
        'rows': rows,
    }


def calcular_predicciones_financieras(limit: int = 8) -> dict:
    reports = list(
        ReporteFinanciero.objects
        .filter(tipo=ReporteFinanciero.TipoReporte.KPI)
        .order_by('-fecha_fin', '-fecha_creacion')[:limit]
    )
    reports.reverse()

    def series(key: str) -> list[Decimal]:
        return [
            _to_decimal((report.indicadores or {}).get('kpis', {}).get(key))
            for report in reports
        ]

    items = []
    for key, label, prefix, suffix in [
        ('flujo_caja', 'Flujo de caja previsto', '$', ''),
        ('rentabilidad', 'Rentabilidad prevista', '$', ''),
        ('rentabilidad_pct', 'Margen previsto', '', '%'),
    ]:
        values = series(key)
        forecast, confidence, detail = _linear_forecast(values)
        if len(values) < 3:
            forecast = _weighted_average(values)
            confidence = Decimal('45') if values else ZERO
            detail = 'Promedio ponderado hasta reunir mas historial.'
        status = 'verde'
        if key in {'flujo_caja', 'rentabilidad'} and forecast < 0:
            status = 'rojo'
        elif key == 'rentabilidad_pct' and forecast < Decimal('5'):
            status = 'rojo'
        elif key == 'rentabilidad_pct' and forecast < Decimal('12'):
            status = 'amarillo'
        items.append({
            'key': key,
            'label': label,
            'value': _round(forecast),
            'prefix': prefix,
            'suffix': suffix,
            'confidence': _round(confidence, '0.1'),
            'status': status,
            'detail': detail,
        })

    return {
        'enabled': bool(reports),
        'samples': len(reports),
        'method': 'Regresion lineal simple sobre reportes financieros',
        'predictions': items,
    }


def analizar_riesgo_inventario(days: int = 30, horizon_days: int = 14, limit: int = 8) -> dict:
    end_date = timezone.localdate()
    start_date = end_date - timedelta(days=days - 1)

    consumption_rows = (
        SalidaLineaDetalle.objects
        .filter(salida__fecha_salida__range=(start_date, end_date))
        .values('sku', 'descripcion', 'material_id')
        .annotate(consumo=Sum('cantidad_enviada'))
        .order_by('-consumo')[:50]
    )
    stock_rows = (
        InventarioAlmacen.objects
        .filter(stock_actual__gt=0)
        .values('material_id', 'material__sku', 'material__nombre')
        .annotate(stock=Sum('stock_actual'))
    )
    stock_by_material = {row['material_id']: _to_decimal(row['stock']) for row in stock_rows if row['material_id']}
    stock_by_sku = {row['material__sku']: _to_decimal(row['stock']) for row in stock_rows if row['material__sku']}

    risks = []
    for row in consumption_rows:
        sku = row['sku']
        material_id = row['material_id']
        consumo = _to_decimal(row['consumo'])
        consumo_diario = _safe_div(consumo, Decimal(days))
        demanda_proyectada = consumo_diario * Decimal(horizon_days)
        stock = stock_by_material.get(material_id, stock_by_sku.get(sku, ZERO))
        cobertura_dias = _safe_div(stock, consumo_diario) if consumo_diario > 0 else Decimal('999')
        faltante = max(demanda_proyectada - stock, ZERO)
        status = 'verde'
        if faltante > 0 or cobertura_dias < Decimal('7'):
            status = 'rojo'
        elif cobertura_dias < Decimal(str(horizon_days)):
            status = 'amarillo'
        risks.append({
            'sku': sku,
            'descripcion': row['descripcion'],
            'stock': _round(stock),
            'consumo_diario': _round(consumo_diario),
            'demanda_proyectada': _round(demanda_proyectada),
            'cobertura_dias': _round(cobertura_dias),
            'faltante': _round(faltante),
            'status': status,
        })

    risks.sort(key=lambda item: ({'rojo': 0, 'amarillo': 1, 'verde': 2}[item['status']], item['cobertura_dias']))

    return {
        'enabled': bool(consumption_rows),
        'days': days,
        'horizon_days': horizon_days,
        'method': 'Consumo diario aprendido por historial reciente',
        'risks': risks[:limit],
    }
