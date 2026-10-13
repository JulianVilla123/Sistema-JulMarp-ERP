from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Count, Q, Sum
from django.utils import timezone

from .models import (
    CatalogoMetricaPredictiva,
    CostoHoraMaquina,
    InformeValidacionDefectoQA,
    InventarioAlmacen,
    Material,
    OrdenCompra,
    OrdenCompraDetalle,
    ProveedorMaterialPrecio,
    OrdenFabricacion,
    ReclamoCliente,
    RecepcionMaterial,
    RecepcionMaterialDetalle,
    ReporteFinanciero,
    ReporteKPIProduccion,
    RegistroParoMantenimiento,
    RegistroScrapDefecto,
    RegistroPrediccion,
    RegistroUsoRecursoProduccion,
    SalidaLineaDetalle,
)

ZERO = Decimal('0')
ONE = Decimal('1')
VERSION_MODELO_ACTUAL = '1.1'


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


def _confidence_label(samples: int, confidence: Decimal) -> tuple[str, str]:
    if samples < 4:
        return 'Baja', 'amarillo'
    if confidence >= Decimal('70') and samples >= 8:
        return 'Alta', 'verde'
    if confidence >= Decimal('45'):
        return 'Media', 'amarillo'
    return 'Baja', 'rojo'


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


def _calidad_dataset_mfg(reports: list[ReporteKPIProduccion]) -> dict:
    metric_fields = [
        'disponibilidad',
        'rendimiento',
        'calidad',
        'oee',
        'tasa_rechazo',
        'cumplimiento_ordenes',
        'utilizacion_recursos',
    ]
    dates = [report.fecha_fin for report in reports]
    unique_dates = set(dates)
    zero_counts = {
        field: sum(1 for report in reports if _to_decimal(getattr(report, field)) == ZERO)
        for field in metric_fields
    }
    return {
        'muestras': len(reports),
        'periodos_unicos': len(unique_dates),
        'duplicados_periodo': max(0, len(dates) - len(unique_dates)),
        'fecha_inicio': min(dates).isoformat() if dates else None,
        'fecha_fin': max(dates).isoformat() if dates else None,
        'metricas': len(metric_fields),
        'ceros_por_metrica': zero_counts,
        'calidad': 'alta' if len(reports) >= 8 and len(unique_dates) == len(dates) else ('media' if len(reports) >= 4 else 'baja'),
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
        forecast_reliable = len(values) >= 4
        confidence_label, confidence_status = _confidence_label(len(values), evaluation['confidence'])
        latest_value = values[-1] if values else None
        change = (forecast - latest_value) if forecast is not None and latest_value is not None else None
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

        detail = 'Se necesitan al menos 4 periodos para mostrar un pronostico confiable.' if not forecast_reliable else 'Pronostico contrastado con periodos historicos.'
        predictions.append({
            'code': field_name,
            'label': label,
            'value': _round(forecast) if forecast is not None else None,
            'lower': _round(lower) if lower is not None else None,
            'upper': _round(upper) if upper is not None else None,
            'unit': unit,
            'confidence': evaluation['confidence'],
            'confidence_label': confidence_label,
            'confidence_status': confidence_status,
            'mae': evaluation['mae'],
            'mape': evaluation['mape'],
            'skill': evaluation['skill'],
            'backtests': evaluation['backtests'],
            'status': status,
            'display_status': status if forecast_reliable else 'amarillo',
            'detail': detail,
            'action': action_by_metric[field_name],
            'enough_data': forecast_reliable,
            'forecast_reliable': forecast_reliable,
            'latest_value': _round(latest_value) if latest_value is not None else None,
            'change': _round(change) if change is not None else None,
            'change_positive': change is not None and change > ZERO,
            'has_value': forecast is not None,
            'has_skill': evaluation['skill'] is not None,
            'has_mape': evaluation['mape'] is not None,
        })

        forecast_series = [None for _ in values]
        if values and forecast_values and forecast_reliable:
            forecast_series[-1] = _float_or_none(values[-1])
        forecast_series.extend([_float_or_none(value) if forecast_reliable else None for value in forecast_values])
        trend_charts.append({
            'code': field_name,
            'label': label,
            'unit': unit,
            'labels': labels + forecast_labels[:len(forecast_values)],
            'history': [_float_or_none(value) for value in values] + [None for _ in forecast_values],
            'forecast': forecast_series,
            'lower': [None for _ in values] + [_float_or_none(max(ZERO, value - residual_scale * Decimal(str(week ** 0.5))) if nonnegative else value - residual_scale * Decimal(str(week ** 0.5))) if forecast_reliable else None for week, value in enumerate(forecast_values, start=1)],
            'upper': [None for _ in values] + [_float_or_none(min(Decimal('100'), value + residual_scale * Decimal(str(week ** 0.5))) if bounded else value + residual_scale * Decimal(str(week ** 0.5))) if forecast_reliable else None for week, value in enumerate(forecast_values, start=1)],
            'confidence': float(evaluation['confidence']),
            'status': status,
            'bounded': bounded,
        })

    alerts = []
    improving = []
    for item in predictions:
        if item['value'] is None:
            continue
        if item['forecast_reliable'] and item['status'] == 'rojo':
            alerts.append({
                'status': 'rojo',
                'label': item['label'],
                'value': f"{item['value']}{item['unit']}",
                'message': item['action'],
            })
        elif len(reports) > 1:
            start_value = metric_value(reports[0], item['code'])
            end_value = metric_value(reports[-1], item['code'])
            delta = end_value - start_value
            if abs(delta) >= max(abs(start_value) * Decimal('0.10'), Decimal('1')):
                direction = 'subio' if delta > 0 else 'bajo'
                improving.append(f"{item['label']} {direction} {abs(_round(delta))}{item['unit']} entre el primer y ultimo periodo.")

    if len(reports) < 4:
        data_quality = f'Datos insuficientes para un pronostico confiable: {len(reports)} de 4 periodos minimos.'
    elif len(reports) < 8:
        data_quality = f"Historial limitado: {len(reports)} semanas con reportes. La proyeccion es orientativa."
    else:
        data_quality = f"Historial de {len(reports)} semanas con reportes disponible."
    explanation = improving[:3]
    if not explanation:
        explanation = ['Sin cambios relevantes detectados.']

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
        'data_quality_details': _calidad_dataset_mfg(reports),
        'backtest_periods': max(0, len(reports) - 3),
    }


def calcular_predicciones_mfg_segmentadas(
    fecha_inicio=None,
    fecha_fin=None,
    horizon_weeks: int = 4,
    min_samples: int = 2,
) -> dict:
    end_date = fecha_fin or timezone.localdate()
    start_date = fecha_inicio or (end_date - timedelta(days=83))
    horizon_weeks = max(1, min(int(horizon_weeks or 1), 12))
    orders = list(
        OrdenFabricacion.objects
        .filter(
            fecha_fin_real__date__range=(start_date, end_date),
            estado=OrdenFabricacion.EstadoOF.COMPLETADA,
        )
        .select_related('bom')
        .order_by('fecha_fin_real')
    )
    grouped = {}
    for order in orders:
        week_start = order.fecha_fin_real.date() - timedelta(days=order.fecha_fin_real.date().weekday())
        dimensions = {
            'LINEA': order.linea_produccion or 'Sin línea',
            'PRODUCTO': order.bom.producto or 'Sin producto',
            'TURNO': order.turno or 'Sin turno',
        }
        for dimension, segment in dimensions.items():
            grouped.setdefault((dimension, segment), {})
            grouped[(dimension, segment)][week_start] = (
                grouped[(dimension, segment)].get(week_start, ZERO)
                + _to_decimal(order.cantidad_producida)
            )

    segments = []
    for (dimension, segment), values_by_week in sorted(grouped.items()):
        weeks = sorted(values_by_week)
        values = [values_by_week[week] for week in weeks]
        if len(values) < min_samples:
            continue
        forecast_values, _, _ = _linear_forecast(values, steps=horizon_weeks)
        evaluation = _evaluate_forecast(values, horizon_weeks)
        current = values[-1]
        forecast = forecast_values[-1] if forecast_values else None
        change = forecast - current if forecast is not None else None
        if forecast is None:
            status = 'amarillo'
        elif forecast < current * Decimal('0.90'):
            status = 'rojo'
        elif forecast < current * Decimal('0.97'):
            status = 'amarillo'
        else:
            status = 'verde'
        confidence_label, confidence_status = _confidence_label(len(values), evaluation['confidence'])
        segments.append({
            'dimension': dimension,
            'segmento': segment,
            'muestras': len(values),
            'periodo_inicio': weeks[0],
            'periodo_fin': weeks[-1],
            'valor_actual': _round(current),
            'valor_predicho': _round(forecast) if forecast is not None else None,
            'cambio': _round(change) if change is not None else None,
            'confianza': evaluation['confidence'],
            'confianza_label': confidence_label,
            'confianza_status': confidence_status,
            'estado': status,
            'error_medio': evaluation['mae'],
            'accion': 'Revisar capacidad, programación y causas de variación del segmento.',
        })
    return {
        'enabled': bool(segments),
        'horizon_weeks': horizon_weeks,
        'segments': segments,
        'samples': len(orders),
        'data_quality': 'alta' if segments and min(item['muestras'] for item in segments) >= 8 else 'media' if segments else 'baja',
    }


def registrar_predicciones(
    *,
    departamento: str,
    alcance: str = 'GLOBAL',
    segmento: str = 'GLOBAL',
    fecha_inicio,
    fecha_fin,
    horizonte: int,
    forecast: dict,
    usuario=None,
    modelo: str = 'Tendencia lineal',
    version_modelo: str = VERSION_MODELO_ACTUAL,
) -> int:
    catalog_defaults = {
        'oee': (85, 70, 60, True),
        'disponibilidad': (90, 80, 70, True),
        'rendimiento': (85, 70, 60, True),
        'calidad': (98, 95, 90, True),
        'unidades_totales': (None, None, None, True),
        'tiempo_ciclo_promedio': (None, None, None, False),
        'tasa_rechazo': (3, 6, 10, False),
        'cumplimiento_ordenes': (90, 80, 70, True),
        'variacion_costos': (5, 10, 15, False),
        'costo_real': (None, None, None, False),
        'costo_planificado': (None, None, None, False),
        'utilizacion_recursos': (85, 70, 60, True),
    }
    registros = 0
    for item in forecast.get('predictions', []):
        objective, yellow, red, higher_is_better = catalog_defaults.get(item['code'], (None, None, None, True))
        CatalogoMetricaPredictiva.objects.update_or_create(
            departamento=departamento,
            codigo=item['code'],
            defaults={
                'nombre': item['label'],
                'unidad': item.get('unit', ''),
                'objetivo': objective,
                'umbral_amarillo': yellow,
                'umbral_rojo': red,
                'mayor_es_mejor': higher_is_better,
                'modelo_recomendado': modelo,
                'accion_default': item.get('action', ''),
                'activo': True,
            },
        )
        RegistroPrediccion.objects.update_or_create(
            departamento=departamento,
            alcance=alcance,
            segmento=segmento,
            codigo_metrica=item['code'],
            periodo_inicio=fecha_inicio,
            periodo_fin=fecha_fin,
            fecha_objetivo=fecha_fin + timedelta(days=7 * horizonte),
            horizonte=horizonte,
            modelo=modelo,
            version_modelo=version_modelo,
            defaults={
                'nombre_metrica': item['label'],
                'valor_actual': item.get('latest_value'),
                'valor_predicho': item.get('value'),
                'limite_inferior': item.get('lower'),
                'limite_superior': item.get('upper'),
                'confianza': item.get('confidence'),
                'estado': item.get('status', 'amarillo'),
                'accion_recomendada': item.get('action', ''),
                'creado_por': usuario,
                'metadatos': {
                    'unidad': item.get('unit', ''),
                    'muestras': forecast.get('samples', 0),
                    'backtests': item.get('backtests', 0),
                    'mape': _float_or_none(item.get('mape')),
                    'skill': _float_or_none(item.get('skill')),
                    'pronostico_confiable': item.get('forecast_reliable', False),
                    'calidad_dataset': forecast.get('data_quality_details', {}),
                },
            },
        )
        registros += 1
    return registros


def evaluar_predicciones(
    *,
    departamento: str,
    alcance: str = 'GLOBAL',
    segmento: str = 'GLOBAL',
    usuario=None,
    tolerancia_dias: int = 3,
) -> int:
    evaluadas = 0
    metric_fields = {
        'oee',
        'disponibilidad',
        'rendimiento',
        'calidad',
        'unidades_totales',
        'tiempo_ciclo_promedio',
        'tasa_rechazo',
        'cumplimiento_ordenes',
        'variacion_costos',
        'costo_real',
        'costo_planificado',
        'utilizacion_recursos',
        'flujo_caja',
        'rentabilidad',
        'rentabilidad_pct',
        'oc_pendientes',
        'impuestos_pendientes',
    }
    pendientes = RegistroPrediccion.objects.filter(
        departamento=departamento,
        alcance=alcance,
        segmento=segmento,
        resultado_real__isnull=True,
        fecha_objetivo__lte=timezone.localdate(),
        codigo_metrica__in=metric_fields,
    )
    for registro in pendientes:
        fecha_rango = (
            registro.fecha_objetivo - timedelta(days=tolerancia_dias),
            registro.fecha_objetivo + timedelta(days=tolerancia_dias),
        )
        if departamento == 'Finanzas':
            reportes = ReporteFinanciero.objects.filter(
                tipo=ReporteFinanciero.TipoReporte.KPI,
                fecha_fin__range=fecha_rango,
            ).order_by('fecha_fin', '-fecha_creacion')
        else:
            reportes = ReporteKPIProduccion.objects.filter(
                fecha_fin__range=fecha_rango
            ).order_by('fecha_fin', '-fecha_generacion')
        reporte = reportes.first()
        if reporte is None:
            continue
        if departamento == 'Finanzas':
            section = 'produccion' if registro.codigo_metrica in {'costo_real', 'costo_planificado', 'variacion_costos'} else 'kpis'
            resultado = _to_decimal((reporte.indicadores or {}).get(section, {}).get(registro.codigo_metrica))
        elif registro.codigo_metrica == 'unidades_totales':
            resultado = _to_decimal((reporte.detalle or {}).get('unidades_totales'))
        else:
            resultado = _to_decimal(getattr(reporte, registro.codigo_metrica))
        if registro.valor_predicho is None:
            continue
        registro.resultado_real = _round(resultado)
        registro.error_real = _round(abs(resultado - registro.valor_predicho))
        registro.fecha_evaluacion = timezone.now()
        registro.metadatos = {
            **(registro.metadatos or {}),
            'evaluado_por': getattr(usuario, 'username', None),
            'reporte_resultado_id': reporte.id,
        }
        registro.save(update_fields=['resultado_real', 'error_real', 'fecha_evaluacion', 'metadatos', 'fecha_actualizacion'])
        evaluadas += 1
    return evaluadas


def monitorear_salud_predictiva(days: int = 180) -> dict:
    """Resume error, pendientes, deriva y versiones de las predicciones recientes."""
    end_date = timezone.localdate()
    start_date = end_date - timedelta(days=max(days - 1, 1))
    rows = list(RegistroPrediccion.objects.filter(fecha_creacion__date__range=(start_date, end_date)))
    departments = sorted({row.departamento for row in rows})
    details = []
    for department in departments:
        department_rows = [row for row in rows if row.departamento == department]
        evaluated = [row for row in department_rows if row.resultado_real is not None and row.error_real is not None]
        due_pending = [row for row in department_rows if row.resultado_real is None and row.fecha_objetivo and row.fecha_objetivo <= end_date]
        percentage_errors = [
            abs(_to_decimal(row.error_real)) / max(abs(_to_decimal(row.valor_predicho)), ONE) * Decimal('100')
            for row in evaluated
        ]
        mean_error = _safe_div(sum((_to_decimal(row.error_real) for row in evaluated), ZERO), Decimal(len(evaluated))) if evaluated else ZERO
        mean_percentage_error = _safe_div(sum(percentage_errors, ZERO), Decimal(len(percentage_errors))) if percentage_errors else None
        if mean_percentage_error is not None and mean_percentage_error >= Decimal('25'):
            status = 'rojo'
            action = 'Revisar deriva del modelo y recalibrar con datos recientes.'
        elif due_pending or len(evaluated) < 4:
            status = 'amarillo'
            action = 'Evaluar periodos vencidos y reunir más resultados reales.'
        else:
            status = 'verde'
            action = 'Continuar monitoreando el error contra resultados reales.'
        details.append({
            'departamento': department,
            'predicciones': len(department_rows),
            'evaluadas': len(evaluated),
            'pendientes_vencidas': len(due_pending),
            'error_medio': _round(mean_error),
            'mape': _round(mean_percentage_error, '0.1') if mean_percentage_error is not None else None,
            'versiones': sorted({row.version_modelo for row in department_rows}),
            'status': status,
            'accion': action,
        })
    details.sort(key=lambda item: {'rojo': 0, 'amarillo': 1, 'verde': 2}.get(item['status'], 2))
    return {
        'enabled': bool(rows),
        'fecha_inicio': start_date,
        'fecha_fin': end_date,
        'predicciones': len(rows),
        'evaluadas': sum(item['evaluadas'] for item in details),
        'pendientes_vencidas': sum(item['pendientes_vencidas'] for item in details),
        'deriva_detectada': any(item['status'] == 'rojo' for item in details),
        'departamentos': details,
        'alerts': [
            {
                'status': item['status'],
                'label': item['departamento'],
                'message': item['accion'],
            }
            for item in details if item['status'] in {'rojo', 'amarillo'}
        ],
    }


def ejecutar_ciclo_predictivo(usuario=None, evaluar=True) -> dict:
    """Ejecuta un ciclo operativo reproducible para todos los módulos predictivos."""
    registrados = {}
    mfg_reports = list(ReporteKPIProduccion.objects.order_by('fecha_fin', 'fecha_generacion'))
    mfg_forecast = calcular_predicciones_mfg(horizon_weeks=1)
    if mfg_reports:
        registrados['MFG'] = registrar_predicciones(
            departamento='MFG',
            fecha_inicio=mfg_reports[0].fecha_inicio,
            fecha_fin=mfg_reports[-1].fecha_fin,
            horizonte=1,
            forecast=mfg_forecast,
            usuario=usuario,
        )
    else:
        registrados['MFG'] = 0

    inventario = calcular_predicciones_inventario()
    registrados['Inventario'] = registrar_predicciones_inventario(inventario, usuario=usuario)
    calidad = calcular_predicciones_calidad()
    registrados['QA'] = registrar_predicciones_calidad(calidad, usuario=usuario)
    mantenimiento = calcular_predicciones_mantenimiento()
    registrados['Mantenimiento'] = registrar_predicciones_mantenimiento(mantenimiento, usuario=usuario)
    finanzas = calcular_predicciones_financieras()
    registrados['Finanzas'] = registrar_predicciones_financieras(finanzas, usuario=usuario)

    evaluadas = {}
    if evaluar:
        evaluadas['MFG'] = evaluar_predicciones(departamento='MFG', usuario=usuario)
        evaluadas['Finanzas'] = evaluar_predicciones(departamento='Finanzas', usuario=usuario)
    return {
        'version_modelo': VERSION_MODELO_ACTUAL,
        'ejecutado_en': timezone.now(),
        'registrados': registrados,
        'evaluadas': evaluadas,
        'salud': monitorear_salud_predictiva(),
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


def calcular_predicciones_financieras(limit: int = 12, horizon_weeks: int = 4,
                                      min_samples: int = 4) -> dict:
    """Pronostica finanzas y planeación a partir de reportes KPI históricos."""
    reports_raw = list(
        ReporteFinanciero.objects
        .filter(tipo=ReporteFinanciero.TipoReporte.KPI)
        .order_by('fecha_fin', 'fecha_creacion')
    )
    # Un mismo periodo puede recalcularse varias veces; para ML se conserva
    # únicamente la versión más reciente de cada periodo.
    reports_by_period = {}
    for report in reports_raw:
        reports_by_period[(report.fecha_inicio, report.fecha_fin)] = report
    reports = sorted(reports_by_period.values(), key=lambda report: (report.fecha_fin, report.fecha_creacion))[-limit:]

    metric_defs = [
        ('flujo_caja', 'Flujo de caja previsto', 'kpis', '$', '', 'negative'),
        ('rentabilidad', 'Rentabilidad prevista', 'kpis', '$', '', 'negative'),
        ('rentabilidad_pct', 'Margen previsto', 'kpis', '', '%', 'margin'),
        ('costo_real', 'Costo real previsto', 'produccion', '$', '', 'cost'),
        ('costo_planificado', 'Costo planificado previsto', 'produccion', '$', '', 'neutral'),
        ('variacion_costos', 'Variación de costos prevista', 'produccion', '$', '', 'cost_variation'),
        ('oc_pendientes', 'Órdenes de compra pendientes', 'kpis', '', '', 'count'),
        ('impuestos_pendientes', 'Impuestos pendientes', 'kpis', '', '', 'count'),
    ]

    def series(key: str, section: str) -> list[Decimal]:
        return [_to_decimal((report.indicadores or {}).get(section, {}).get(key)) for report in reports]

    def status_for(kind: str, value: Decimal | None) -> str:
        if value is None:
            return 'amarillo'
        if kind == 'negative':
            return 'rojo' if value < 0 else 'verde'
        if kind == 'margin':
            return 'rojo' if value < Decimal('5') else ('amarillo' if value < Decimal('12') else 'verde')
        if kind == 'cost_variation':
            return 'rojo' if value > 0 else 'verde'
        if kind == 'count':
            return 'rojo' if value >= 5 else ('amarillo' if value > 0 else 'verde')
        return 'verde'

    items = []
    for key, label, section, prefix, suffix, kind in metric_defs:
        values = series(key, section)
        if not values:
            forecast_values = []
            confidence = ZERO
            detail = 'Sin reportes históricos para generar un pronóstico.'
            evaluation = {'mae': ZERO, 'mape': None, 'backtests': 0}
        else:
            forecast_values, confidence, detail = _linear_forecast(values, steps=horizon_weeks)
            if len(values) < 3:
                forecast_values = [_weighted_average(values) for _ in range(horizon_weeks)]
                confidence = Decimal('45')
                detail = 'Historial limitado; se utiliza promedio ponderado.'
            evaluation = _evaluate_forecast(values, horizon_weeks)
        forecast_values = [_round(value) for value in forecast_values]
        forecast = forecast_values[-1] if forecast_values else None
        latest = values[-1] if values else None
        confidence_label, confidence_status = _confidence_label(len(values), confidence)
        status = status_for(kind, forecast)
        display_status = status if len(values) >= min_samples else 'amarillo'
        action = {
            'rojo': 'Revisar flujo, costos y compromisos antes del siguiente cierre financiero.',
            'amarillo': 'Reunir más periodos y validar la proyección con Finanzas.',
            'verde': 'Mantener seguimiento mensual y comparar contra el resultado real.',
        }[display_status]
        items.append({
            'key': key,
            'label': label,
            'value': forecast,
            'latest_value': _round(latest) if latest is not None else None,
            'forecast_values': forecast_values,
            'prefix': prefix,
            'suffix': suffix,
            'confidence': _round(confidence, '0.1'),
            'confidence_label': confidence_label,
            'confidence_status': confidence_status,
            'status': status,
            'display_status': display_status,
            'forecast_reliable': len(values) >= min_samples,
            'samples': len(values),
            'backtests': evaluation['backtests'],
            'mape': evaluation['mape'],
            'action': action,
            'detail': detail,
        })

    item_by_key = {item['key']: item for item in items}
    alerts = [
        {
            'status': item['display_status'],
            'label': item['label'],
            'value': f"{item['prefix']}{item['value'] or 0}{item['suffix']}",
            'message': item['action'],
        }
        for item in items if item['display_status'] in {'rojo', 'amarillo'}
    ]
    alerts.sort(key=lambda item: {'rojo': 0, 'amarillo': 1}.get(item['status'], 2))
    start_date = reports[0].fecha_inicio if reports else timezone.localdate()
    end_date = reports[-1].fecha_fin if reports else timezone.localdate()
    return {
        'enabled': bool(reports),
        'samples': len(reports),
        'min_samples': min_samples,
        'horizon_weeks': horizon_weeks,
        'fecha_inicio': start_date,
        'fecha_fin': end_date,
        'method': 'Tendencia financiera sobre reportes KPI por periodo',
        'data_quality': 'alta' if len(reports) >= 8 else ('media' if len(reports) >= 4 else 'baja'),
        'predictions': items,
        'alerts': alerts[:6],
        'summary': {
            'flujo_caja': item_by_key.get('flujo_caja', {}).get('value'),
            'rentabilidad': item_by_key.get('rentabilidad', {}).get('value'),
            'margen': item_by_key.get('rentabilidad_pct', {}).get('value'),
            'costo_real': item_by_key.get('costo_real', {}).get('value'),
            'variacion_costos': item_by_key.get('variacion_costos', {}).get('value'),
            'oc_pendientes': item_by_key.get('oc_pendientes', {}).get('value'),
            'impuestos_pendientes': item_by_key.get('impuestos_pendientes', {}).get('value'),
        },
        'trend': [
            {'label': report.fecha_fin.strftime('%d/%m'), 'value': _to_decimal((report.indicadores or {}).get('kpis', {}).get('flujo_caja'))}
            for report in reports
        ],
        'trend_forecast': item_by_key.get('flujo_caja', {}).get('forecast_values', []),
    }


def registrar_predicciones_financieras(forecast: dict, usuario=None) -> int:
    """Persiste predicciones financieras en el contrato común de ML."""
    if not forecast.get('enabled'):
        return 0
    model = 'Tendencia financiera sobre reportes KPI'
    catalog = {
        'flujo_caja': ('Flujo de caja previsto', '$', True),
        'rentabilidad': ('Rentabilidad prevista', '$', True),
        'rentabilidad_pct': ('Margen previsto', '%', True),
        'costo_real': ('Costo real previsto', '$', False),
        'costo_planificado': ('Costo planificado previsto', '$', False),
        'variacion_costos': ('Variación de costos prevista', '$', False),
        'oc_pendientes': ('Órdenes de compra pendientes', 'órdenes', False),
        'impuestos_pendientes': ('Impuestos pendientes', 'declaraciones', False),
    }
    target_date = forecast['fecha_fin'] + timedelta(weeks=forecast['horizon_weeks'])
    registered = 0
    for item in forecast.get('predictions', []):
        code = item['key']
        name, unit, higher_is_better = catalog[code]
        action = item['action']
        CatalogoMetricaPredictiva.objects.update_or_create(
            departamento='Finanzas', codigo=code,
            defaults={
                'nombre': name,
                'unidad': unit,
                'mayor_es_mejor': higher_is_better,
                'modelo_recomendado': model,
                'accion_default': action,
                'activo': True,
            },
        )
        RegistroPrediccion.objects.update_or_create(
            departamento='Finanzas', alcance='GLOBAL', segmento='GLOBAL', codigo_metrica=code,
            periodo_inicio=forecast['fecha_inicio'], periodo_fin=forecast['fecha_fin'],
            fecha_objetivo=target_date, horizonte=forecast['horizon_weeks'], modelo=model,
            version_modelo=VERSION_MODELO_ACTUAL,
            defaults={
                'nombre_metrica': name,
                'valor_actual': item['latest_value'],
                'valor_predicho': item['value'],
                'limite_inferior': None,
                'limite_superior': None,
                'confianza': item['confidence'],
                'estado': item['display_status'],
                'accion_recomendada': action,
                'creado_por': usuario,
                'metadatos': {
                    'muestras': item['samples'],
                    'backtests': item['backtests'],
                    'mape': _float_or_none(item['mape']),
                    'pronostico_confiable': item['forecast_reliable'],
                    'calidad_dataset': forecast['data_quality'],
                },
            },
        )
        registered += 1
    return registered


def calcular_predicciones_inventario(
    days: int = 90,
    horizon_days: int = 30,
    limit: int = 12,
    min_samples: int = 4,
) -> dict:
    """Pronostica demanda semanal y convierte el resultado en una política de inventario.

    La salida está orientada a decisiones: demanda futura, punto de reorden,
    agotamiento, órdenes en tránsito y recomendación de compra por material.
    Se conserva el umbral de cuatro periodos antes de marcar un pronóstico como
    confiable, aunque el sistema muestra una señal preventiva cuando el historial
    todavía es corto.
    """
    days = max(28, int(days or 90))
    horizon_days = max(7, int(horizon_days or 30))
    limit = max(1, int(limit or 12))
    min_samples = max(4, int(min_samples or 4))
    end_date = timezone.localdate()
    start_date = end_date - timedelta(days=days - 1)
    horizon_weeks = max(1, (horizon_days + 6) // 7)

    daily_rows = list(
        SalidaLineaDetalle.objects
        .filter(salida__fecha_salida__range=(start_date, end_date))
        .values('sku', 'descripcion', 'material_id', 'salida__fecha_salida')
        .annotate(consumo=Sum('cantidad_enviada'))
        .order_by('sku', 'salida__fecha_salida')
    )
    weekly_by_sku = defaultdict(lambda: defaultdict(lambda: ZERO))
    material_ids_by_sku = {}
    descriptions_by_sku = {}
    for row in daily_rows:
        sku = (row['sku'] or '').strip().upper()
        movement_date = row['salida__fecha_salida']
        if not sku or not movement_date:
            continue
        week_start = movement_date - timedelta(days=movement_date.weekday())
        weekly_by_sku[sku][week_start] += _to_decimal(row['consumo'])
        material_ids_by_sku.setdefault(sku, row['material_id'])
        descriptions_by_sku.setdefault(sku, row['descripcion'] or sku)

    material_rows = Material.objects.filter(sku__in=list(weekly_by_sku)).values('sku', 'nombre', 'um')
    material_catalog = {row['sku']: row for row in material_rows}

    stock_rows = (
        InventarioAlmacen.objects
        .values('material__sku')
        .annotate(stock=Sum('stock_actual'))
    )
    stock_by_sku = {
        (row['material__sku'] or '').upper(): _to_decimal(row['stock'])
        for row in stock_rows
        if row['material__sku']
    }

    pending_states = [
        OrdenCompra.EstadoOrden.APROBADA,
        OrdenCompra.EstadoOrden.ENVIADA,
        OrdenCompra.EstadoOrden.PARCIAL,
    ]
    pending_rows = (
        OrdenCompraDetalle.objects
        .filter(orden__estado__in=pending_states)
        .values('sku')
        .annotate(total=Sum('cantidad_pedida'))
    )
    pending_by_sku = {
        (row['sku'] or '').upper(): _to_decimal(row['total'])
        for row in pending_rows
        if row['sku']
    }

    lead_time_by_sku = defaultdict(list)
    lead_time_rows = (
        OrdenCompraDetalle.objects
        .exclude(orden__estado=OrdenCompra.EstadoOrden.CANCELADA)
        .values('sku', 'orden__tiempo_entrega_estimado_dias')
    )
    for row in lead_time_rows:
        sku = (row['sku'] or '').upper()
        lead_time = int(row['orden__tiempo_entrega_estimado_dias'] or 0)
        if sku and lead_time > 0:
            lead_time_by_sku[sku].append(lead_time)

    best_supplier_by_sku = {}
    price_rows = (
        ProveedorMaterialPrecio.objects
        .filter(proveedor__activo=True, material__activo=True, material__sku__in=list(weekly_by_sku))
        .values('material__sku', 'proveedor__nombre', 'precio_unitario')
        .order_by('material__sku', 'precio_unitario', 'proveedor__nombre')
    )
    for row in price_rows:
        sku = row['material__sku']
        if sku not in best_supplier_by_sku:
            best_supplier_by_sku[sku] = {
                'nombre': row['proveedor__nombre'],
                'precio': _to_decimal(row['precio_unitario']),
            }

    all_risks = []
    for sku, weekly_data in weekly_by_sku.items():
        weeks = sorted(weekly_data)
        weekly_values = [weekly_data[week] for week in weeks]
        evaluation = _evaluate_forecast(weekly_values, horizon_weeks)
        forecast_values, _, _ = _linear_forecast(weekly_values, steps=horizon_weeks)
        forecast_values = [max(_to_decimal(value), ZERO) for value in forecast_values]
        forecast_reliable = len(weekly_values) >= min_samples
        confidence_label, confidence_status = _confidence_label(len(weekly_values), evaluation['confidence'])

        demand_projected = sum(forecast_values, ZERO)
        latest_weekly_demand = weekly_values[-1] if weekly_values else ZERO
        demand_daily = _safe_div(demand_projected, Decimal(horizon_weeks * 7))
        if demand_daily <= 0:
            demand_daily = _safe_div(latest_weekly_demand, Decimal('7'))

        stock = max(stock_by_sku.get(sku, ZERO), ZERO)
        on_order = max(pending_by_sku.get(sku, ZERO), ZERO)
        lead_times = lead_time_by_sku.get(sku, [])
        lead_time_days = max(1, round(sum(lead_times) / len(lead_times))) if lead_times else 7
        safety_days = max(3, (lead_time_days + 1) // 2)
        reorder_point = demand_daily * Decimal(lead_time_days + safety_days)
        purchase_quantity = max(reorder_point - stock - on_order, ZERO)
        coverage_days = _safe_div(stock, demand_daily) if demand_daily > 0 else None
        stockout_days = coverage_days if coverage_days is not None else None
        stockout_date = None
        if stockout_days is not None:
            stockout_date = end_date + timedelta(days=max(0, int(stockout_days.to_integral_value(rounding=ROUND_HALF_UP))))

        if demand_daily > 0 and (stock <= 0 or stockout_days <= lead_time_days):
            status = 'rojo'
        elif purchase_quantity > 0 or (demand_daily > 0 and stockout_days <= horizon_days):
            status = 'amarillo'
        else:
            status = 'verde'

        supplier = best_supplier_by_sku.get(sku, {})
        supplier_name = supplier.get('nombre') or 'Sin proveedor con precio registrado'
        action = 'Mantener monitoreo semanal; el stock cubre el punto de reorden.'
        if purchase_quantity > 0:
            action = f'Comprar {_round(purchase_quantity)} {material_catalog.get(sku, {}).get("um") or "unidades"} y revisar el proveedor recomendado.'
        elif on_order > 0:
            action = 'Dar seguimiento a la orden de compra en tránsito antes de emitir una nueva.'
        elif status == 'rojo':
            action = 'Confirmar abastecimiento inmediato y revisar el consumo de la línea.'
        if not forecast_reliable:
            action = f'Historial corto ({len(weekly_values)} periodos); validar manualmente antes de comprar. {action}'

        material = material_catalog.get(sku, {})
        all_risks.append({
            'sku': sku,
            'descripcion': material.get('nombre') or descriptions_by_sku.get(sku, sku),
            'um': material.get('um') or '',
            'muestras': len(weekly_values),
            'periodo_inicio': weeks[0] if weeks else start_date,
            'periodo_fin': weeks[-1] if weeks else end_date,
            'stock': _round(stock),
            'en_orden': _round(on_order),
            'consumo_diario': _round(demand_daily),
            'demanda_proyectada': _round(demand_projected),
            'cobertura_dias': _round(coverage_days) if coverage_days is not None else None,
            'punto_reorden': _round(reorder_point),
            'faltante': _round(purchase_quantity),
            'lead_time_dias': lead_time_days,
            'safety_days': safety_days,
            'dias_agotamiento': _round(stockout_days) if stockout_days is not None else None,
            'fecha_agotamiento': stockout_date,
            'proveedor_recomendado': supplier_name,
            'precio_recomendado': _round(supplier.get('precio')) if supplier.get('precio') is not None else None,
            'status': status,
            'display_status': status if forecast_reliable else 'amarillo',
            'confidence': evaluation['confidence'],
            'confidence_label': confidence_label,
            'confidence_status': confidence_status,
            'forecast_reliable': forecast_reliable,
            'mape': evaluation['mape'],
            'mae': evaluation['mae'],
            'backtests': evaluation['backtests'],
            'action': action,
        })

    all_risks.sort(key=lambda item: (
        {'rojo': 0, 'amarillo': 1, 'verde': 2}[item['status']],
        item['dias_agotamiento'] if item['dias_agotamiento'] is not None else Decimal('999999'),
    ))
    recommendations = [item for item in all_risks if item['faltante'] > ZERO or item['status'] == 'rojo']
    quality = 'alta' if all_risks and min(item['muestras'] for item in all_risks) >= 8 else ('media' if all_risks and min(item['muestras'] for item in all_risks) >= 4 else 'baja')

    return {
        'enabled': bool(all_risks),
        'days': days,
        'horizon_days': horizon_days,
        'horizon_weeks': horizon_weeks,
        'fecha_inicio': start_date,
        'fecha_fin': end_date,
        'method': 'Pronóstico semanal de demanda + punto de reorden y tiempo de entrega',
        'data_quality': quality,
        'min_samples': min_samples,
        'samples': max((item['muestras'] for item in all_risks), default=0),
        'risks': all_risks[:limit],
        'purchase_recommendations': recommendations[:limit],
        'summary': {
            'materiales': len(all_risks),
            'criticos': sum(1 for item in all_risks if item['status'] == 'rojo'),
            'por_reordenar': sum(1 for item in all_risks if item['faltante'] > ZERO),
            'cantidad_sugerida_compra': _round(sum((item['faltante'] for item in all_risks), ZERO)),
        },
    }


def registrar_predicciones_inventario(forecast: dict, usuario=None) -> int:
    """Registra demanda y decisiones de inventario en el contrato predictivo común."""
    if not forecast.get('enabled'):
        return 0

    metric_config = {
        'demanda_material': ('Demanda futura por material', 'unidades', True),
        'punto_reorden': ('Punto de reorden', 'unidades', False),
        'riesgo_ruptura': ('Riesgo de ruptura de stock', '%', False),
        'agotamiento_estimado': ('Días para agotamiento', 'días', False),
    }
    modelo = 'Demanda semanal + política de inventario'
    registros = 0
    for item in forecast.get('risks', []):
        sku = item['sku']
        confidence = item['confidence']
        status = item['display_status']
        risk_value = Decimal('100') if item['status'] == 'rojo' else (Decimal('50') if item['status'] == 'amarillo' else ZERO)
        metrics = [
            ('demanda_material', item['demanda_proyectada'], item['consumo_diario'] * Decimal('7'), item['demanda_proyectada']),
            ('punto_reorden', item['punto_reorden'], item['stock'], item['punto_reorden']),
            ('riesgo_ruptura', risk_value, ZERO, Decimal('100')),
            ('agotamiento_estimado', item['dias_agotamiento'], item['dias_agotamiento'], None),
        ]
        for code, predicted, current, upper in metrics:
            name, unit, higher_is_better = metric_config[code]
            CatalogoMetricaPredictiva.objects.update_or_create(
                departamento='Inventario',
                codigo=code,
                defaults={
                    'nombre': name,
                    'unidad': unit,
                    'mayor_es_mejor': higher_is_better,
                    'modelo_recomendado': modelo,
                    'accion_default': item['action'],
                    'activo': True,
                },
            )
            RegistroPrediccion.objects.update_or_create(
                departamento='Inventario',
                alcance='MATERIAL',
                segmento=sku,
                codigo_metrica=code,
                periodo_inicio=item['periodo_inicio'],
                periodo_fin=item['periodo_fin'],
                fecha_objetivo=item['fecha_agotamiento'] or (forecast['fecha_fin'] + timedelta(days=forecast['horizon_days'])),
                horizonte=forecast['horizon_days'],
                modelo=modelo,
                version_modelo=VERSION_MODELO_ACTUAL,
                defaults={
                    'nombre_metrica': f'{name} · {sku}',
                    'valor_actual': current,
                    'valor_predicho': predicted,
                    'limite_inferior': None,
                    'limite_superior': upper,
                    'confianza': confidence,
                    'estado': status,
                    'accion_recomendada': item['action'],
                    'creado_por': usuario,
                    'metadatos': {
                        'unidad': unit,
                        'muestras': item['muestras'],
                        'backtests': item['backtests'],
                        'mape': _float_or_none(item['mape']),
                        'mae': _float_or_none(item['mae']),
                        'calidad_dataset': forecast['data_quality'],
                        'proveedor_recomendado': item['proveedor_recomendado'],
                        'lead_time_dias': item['lead_time_dias'],
                        'en_orden': _float_or_none(item['en_orden']),
                    },
                },
            )
            registros += 1
    return registros


def analizar_riesgo_inventario(days: int = 90, horizon_days: int = 30, limit: int = 12) -> dict:
    """Compatibilidad para las vistas existentes de inventario."""
    return calcular_predicciones_inventario(days=days, horizon_days=horizon_days, limit=limit)


def calcular_predicciones_calidad(
    days: int = 90,
    horizon_weeks: int = 4,
    limit: int = 10,
    min_samples: int = 4,
) -> dict:
    """Analiza riesgo de calidad por producto, línea, causa y proveedor."""
    days = max(28, int(days or 90))
    horizon_weeks = max(1, min(int(horizon_weeks or 4), 12))
    limit = max(1, int(limit or 10))
    min_samples = max(4, int(min_samples or 4))
    end_date = timezone.localdate()
    start_date = end_date - timedelta(days=days - 1)

    orders = list(
        OrdenFabricacion.objects
        .filter(
            estado=OrdenFabricacion.EstadoOF.COMPLETADA,
            fecha_fin_real__date__range=(start_date, end_date),
        )
        .select_related('bom')
    )
    defects = list(
        RegistroScrapDefecto.objects
        .filter(
            Q(fecha_creacion__date__range=(start_date, end_date))
            | Q(orden__fecha_fin_real__date__range=(start_date, end_date))
        )
        .select_related('orden__bom', 'lote__bom')
        .distinct()
    )
    qa_results = {
        row['defecto_id']: row
        for row in InformeValidacionDefectoQA.objects
        .filter(defecto_id__in=[defect.id for defect in defects])
        .values('defecto_id', 'resultado_validacion', 'falla_maquina')
    }

    weekly_total = defaultdict(lambda: {'produced': ZERO, 'defects': ZERO})
    product_totals = defaultdict(lambda: {'produced': ZERO, 'defects': ZERO})
    bom_totals = defaultdict(lambda: {'produced': ZERO, 'defects': ZERO})
    line_totals = defaultdict(lambda: {'produced': ZERO, 'defects': ZERO})
    product_weeks = defaultdict(lambda: defaultdict(lambda: {'produced': ZERO, 'defects': ZERO}))
    bom_weeks = defaultdict(lambda: defaultdict(lambda: {'produced': ZERO, 'defects': ZERO}))
    line_weeks = defaultdict(lambda: defaultdict(lambda: {'produced': ZERO, 'defects': ZERO}))
    product_causes = defaultdict(lambda: defaultdict(lambda: ZERO))
    bom_causes = defaultdict(lambda: defaultdict(lambda: ZERO))
    line_causes = defaultdict(lambda: defaultdict(lambda: ZERO))

    def order_context(order=None, lote=None):
        bom = getattr(order, 'bom', None) or getattr(lote, 'bom', None)
        product = bom.producto if bom else 'Producto no identificado'
        bom_code = bom.codigo if bom else 'Sin BOM'
        line = (getattr(order, 'linea_produccion', '') if order else '') or 'Sin línea capturada'
        return product, bom_code, line

    for order in orders:
        order_date = order.fecha_fin_real.date()
        week = order_date - timedelta(days=order_date.weekday())
        product, bom_code, line = order_context(order=order)
        produced = max(_to_decimal(order.cantidad_producida), ZERO)
        weekly_total[week]['produced'] += produced
        product_totals[product]['produced'] += produced
        bom_totals[bom_code]['produced'] += produced
        line_totals[line]['produced'] += produced
        product_weeks[product][week]['produced'] += produced
        bom_weeks[bom_code][week]['produced'] += produced
        line_weeks[line][week]['produced'] += produced

    cause_totals = defaultdict(lambda: {'defects': ZERO, 'count': 0, 'weeks': set()})
    total_defects = ZERO
    for defect in defects:
        defect_date = defect.orden.fecha_fin_real.date() if defect.orden and defect.orden.fecha_fin_real else defect.fecha_creacion.date()
        week = defect_date - timedelta(days=defect_date.weekday())
        product, bom_code, line = order_context(order=defect.orden, lote=defect.lote)
        quantity = max(_to_decimal(defect.cantidad_defectos), ZERO)
        total_defects += quantity
        weekly_total[week]['defects'] += quantity
        product_totals[product]['defects'] += quantity
        bom_totals[bom_code]['defects'] += quantity
        line_totals[line]['defects'] += quantity
        product_weeks[product][week]['defects'] += quantity
        bom_weeks[bom_code][week]['defects'] += quantity
        line_weeks[line][week]['defects'] += quantity
        cause = (defect.causa or 'Causa no capturada').strip()
        cause_totals[cause]['defects'] += quantity
        cause_totals[cause]['count'] += 1
        cause_totals[cause]['weeks'].add(week)
        product_causes[product][cause] += quantity
        bom_causes[bom_code][cause] += quantity
        line_causes[line][cause] += quantity

    def rate_for(bucket):
        return _bounded_percent(_safe_div(bucket['defects'], bucket['produced']) * Decimal('100')) if bucket['produced'] > 0 else None

    def build_segment(dimension, name, totals, weekly_data, causes):
        weeks = sorted(weekly_data)
        history = [rate_for(weekly_data[week]) for week in weeks]
        history = [value for value in history if value is not None]
        if not history:
            current_rate = None
            predicted_rate = None
            evaluation = {'confidence': ZERO, 'mae': ZERO, 'mape': None, 'backtests': 0}
        else:
            forecast_values, _, _ = _linear_forecast(history, steps=horizon_weeks)
            if len(history) < 3:
                fallback = _weighted_average(history)
                forecast_values = [fallback for _ in range(horizon_weeks)]
            forecast_values = [_bounded_percent(value) for value in forecast_values]
            current_rate = history[-1]
            predicted_rate = forecast_values[-1]
            evaluation = _evaluate_forecast(history, horizon_weeks)

        samples = len(history)
        confidence_label, confidence_status = _confidence_label(samples, evaluation['confidence'])
        forecast_reliable = samples >= min_samples
        status = 'rojo' if predicted_rate is not None and predicted_rate >= Decimal('5') else ('amarillo' if predicted_rate is not None and predicted_rate >= Decimal('2') else 'verde')
        display_status = status if forecast_reliable else 'amarillo'
        if current_rate is None or predicted_rate is None:
            trend = 'sin datos suficientes'
        elif predicted_rate > current_rate * Decimal('1.10'):
            trend = 'subiendo'
        elif predicted_rate < current_rate * Decimal('0.90'):
            trend = 'bajando'
        else:
            trend = 'estable'
        probable_cause = max(causes.items(), key=lambda pair: pair[1])[0] if causes else 'Sin causa dominante'
        if status == 'rojo':
            action = f'Contener y abrir acción correctiva para: {probable_cause}.'
        elif status == 'amarillo':
            action = f'Revisar tendencia y validar la causa: {probable_cause}.'
        else:
            action = 'Mantener el plan de control y monitoreo semanal.'
        if not forecast_reliable:
            action = f'Historial corto ({samples} periodos); validar manualmente. {action}'
        return {
            'dimension': dimension,
            'segmento': name,
            'probabilidad_defecto': _round(predicted_rate) if predicted_rate is not None else None,
            'tasa_actual': _round(current_rate) if current_rate is not None else None,
            'defectos': _round(totals['defects']),
            'produccion': _round(totals['produced']),
            'muestras': samples,
            'confianza': evaluation['confidence'],
            'confianza_label': confidence_label,
            'confianza_status': confidence_status,
            'forecast_reliable': forecast_reliable,
            'status': status,
            'display_status': display_status,
            'tendencia': trend,
            'causa_probable': probable_cause,
            'accion': action,
            'backtests': evaluation['backtests'],
            'mae': evaluation['mae'],
            'mape': evaluation['mape'],
        }

    segments = []
    for product, totals in product_totals.items():
        segments.append(build_segment('PRODUCTO', product, totals, product_weeks[product], product_causes[product]))
    for bom_code, totals in bom_totals.items():
        segments.append(build_segment('BOM', bom_code, totals, bom_weeks[bom_code], bom_causes[bom_code]))
    for line, totals in line_totals.items():
        segments.append(build_segment('LINEA', line, totals, line_weeks[line], line_causes[line]))

    supplier_totals = defaultdict(lambda: {'received': ZERO, 'defects': ZERO, 'samples': 0})
    material_totals = defaultdict(lambda: {'received': ZERO, 'defects': ZERO, 'samples': 0, 'nombre': ''})
    receipt_rows = (
        RecepcionMaterialDetalle.objects
        .filter(recepcion__fecha_recepcion__range=(start_date, end_date))
        .select_related('recepcion', 'material')
    )
    for detail in receipt_rows:
        supplier = (detail.recepcion.proveedor or 'Proveedor no identificado').strip()
        received = max(_to_decimal(detail.cantidad_recibida) or _to_decimal(detail.cantidad_oc), ZERO)
        supplier_totals[supplier]['received'] += received
        supplier_totals[supplier]['samples'] += 1
        if detail.estatus != RecepcionMaterialDetalle.EstatusDetalle.ACEPTADO:
            supplier_totals[supplier]['defects'] += received
        material_sku = (detail.sku or (detail.material.sku if detail.material_id else '') or 'Material no identificado').strip().upper()
        material_totals[material_sku]['nombre'] = detail.descripcion or (detail.material.nombre if detail.material_id else material_sku)
        material_totals[material_sku]['received'] += received
        material_totals[material_sku]['samples'] += 1
        if detail.estatus != RecepcionMaterialDetalle.EstatusDetalle.ACEPTADO:
            material_totals[material_sku]['defects'] += received

    supplier_risks = []
    for supplier, totals in supplier_totals.items():
        probability = _bounded_percent(_safe_div(totals['defects'], totals['received']) * Decimal('100')) if totals['received'] else None
        confidence = min(Decimal('95'), Decimal('35') + Decimal(totals['samples']) * Decimal('8'))
        confidence_label, confidence_status = _confidence_label(totals['samples'], confidence)
        status = 'rojo' if probability is not None and probability >= Decimal('5') else ('amarillo' if probability is not None and probability >= Decimal('2') else 'verde')
        supplier_risks.append({
            'dimension': 'PROVEEDOR',
            'segmento': supplier,
            'probabilidad_defecto': _round(probability) if probability is not None else None,
            'tasa_actual': _round(probability) if probability is not None else None,
            'defectos': _round(totals['defects']),
            'produccion': _round(totals['received']),
            'muestras': totals['samples'],
            'confianza': _round(confidence, '0.1'),
            'confianza_label': confidence_label,
            'confianza_status': confidence_status,
            'forecast_reliable': totals['samples'] >= min_samples,
            'status': status,
            'display_status': status if totals['samples'] >= min_samples else 'amarillo',
            'tendencia': 'estable',
            'causa_probable': 'Recepción con diferencia o rechazo',
            'accion': 'Solicitar plan de acción al proveedor y reforzar inspección de entrada.' if status != 'verde' else 'Mantener frecuencia de inspección actual.',
            'backtests': 0,
            'mae': ZERO,
            'mape': None,
        })

    material_contributors = []
    for material_sku, totals in material_totals.items():
        probability = _bounded_percent(_safe_div(totals['defects'], totals['received']) * Decimal('100')) if totals['received'] else None
        confidence = min(Decimal('95'), Decimal('35') + Decimal(totals['samples']) * Decimal('8'))
        confidence_label, confidence_status = _confidence_label(totals['samples'], confidence)
        status = 'rojo' if probability is not None and probability >= Decimal('5') else ('amarillo' if probability is not None and probability >= Decimal('2') else 'verde')
        material_contributors.append({
            'dimension': 'MATERIAL',
            'segmento': material_sku,
            'nombre': totals['nombre'] or material_sku,
            'probabilidad_defecto': _round(probability) if probability is not None else None,
            'defectos': _round(totals['defects']),
            'muestras': totals['samples'],
            'confianza': _round(confidence, '0.1'),
            'confianza_label': confidence_label,
            'confianza_status': confidence_status,
            'status': status,
            'display_status': status if totals['samples'] >= min_samples else 'amarillo',
            'accion': 'Reforzar inspección del material y solicitar evidencia de causa al proveedor.' if status != 'verde' else 'Mantener el plan de inspección del material.',
        })
    material_contributors.sort(key=lambda item: ({'rojo': 0, 'amarillo': 1, 'verde': 2}[item['status']], -(item['probabilidad_defecto'] or ZERO)))

    def build_cause(cause, data):
        percentage = _safe_div(data['defects'], total_defects) * Decimal('100') if total_defects else ZERO
        return {
            'causa': cause,
            'defectos': _round(data['defects']),
            'porcentaje': _round(percentage),
            'ocurrencias': data['count'],
            'recurrencia_periodos': len(data['weeks']),
            'severidad': 'Alta' if percentage >= Decimal('40') else ('Media' if percentage >= Decimal('15') else 'Baja'),
            'tendencia': 'recurrente' if len(data['weeks']) >= 2 else 'puntual',
            'accion': f'Analizar causa raíz de {cause} y verificar la eficacia de la contención.',
        }

    causes = sorted((build_cause(cause, data) for cause, data in cause_totals.items()), key=lambda item: item['defectos'], reverse=True)
    for cause in causes:
        cause['tipo'] = 'PROCESO'
    week_starts = sorted(weekly_total)
    trend_values = [rate_for(weekly_total[week]) for week in week_starts]
    trend_values = [value for value in trend_values if value is not None]
    trend_forecast, _, _ = _linear_forecast(trend_values, steps=horizon_weeks) if trend_values else ([], ZERO, '')
    if len(trend_values) < 3 and trend_values:
        trend_forecast = [_weighted_average(trend_values) for _ in range(horizon_weeks)]
    trend_forecast = [_bounded_percent(value) for value in trend_forecast]
    trend_evaluation = _evaluate_forecast(trend_values, horizon_weeks) if trend_values else {'confidence': ZERO, 'mae': ZERO, 'mape': None, 'backtests': 0}
    trend_confidence_label, trend_confidence_status = _confidence_label(len(trend_values), trend_evaluation['confidence'])
    total_produced = sum((item['produced'] for item in weekly_total.values()), ZERO)
    overall_rate = _safe_div(total_defects, total_produced) * Decimal('100') if total_produced else None
    claim_count = ReclamoCliente.objects.filter(fecha_creacion__date__range=(start_date, end_date)).count()
    claim_probability = _safe_div(Decimal(claim_count), Decimal(len(orders))) * Decimal('100') if orders else None
    claim_confidence = Decimal('70') if len(orders) >= 8 else Decimal('40')
    claim_label, claim_status = _confidence_label(len(orders), claim_confidence)
    claim_risk_status = 'rojo' if claim_probability is not None and claim_probability >= Decimal('5') else ('amarillo' if claim_probability else 'verde')

    segments.extend(supplier_risks)
    segments.sort(key=lambda item: ({'rojo': 0, 'amarillo': 1, 'verde': 2}[item['status']], -(item['probabilidad_defecto'] or ZERO)))
    alerts = []
    for segment in segments:
        if segment['status'] in {'rojo', 'amarillo'}:
            alerts.append({
                'status': segment['display_status'],
                'label': f"{segment['dimension'].title()} · {segment['segmento']}",
                'value': f"{segment['probabilidad_defecto'] or 0}%",
                'message': segment['accion'],
            })
    for cause in causes[:3]:
        alerts.append({
            'status': 'rojo' if cause['severidad'] == 'Alta' else 'amarillo',
            'label': f"Causa · {cause['causa']}",
            'value': f"{cause['porcentaje']}%",
            'message': cause['accion'],
        })
    if claim_risk_status in {'rojo', 'amarillo'}:
        alerts.append({
            'status': claim_risk_status,
            'label': 'Riesgo de reclamo',
            'value': f"{_round(claim_probability or ZERO)}%",
            'message': 'Revisar lotes liberados y reclamos abiertos antes del siguiente embarque.',
        })
    alerts.sort(key=lambda item: {'rojo': 0, 'amarillo': 1, 'verde': 2}.get(item['status'], 2))
    quality = 'alta' if len(trend_values) >= 8 else ('media' if len(trend_values) >= 4 else 'baja')
    return {
        'enabled': bool(orders or defects or supplier_risks),
        'fecha_inicio': start_date,
        'fecha_fin': end_date,
        'days': days,
        'horizon_weeks': horizon_weeks,
        'min_samples': min_samples,
        'method': 'Tendencia de defectos y tasas de rechazo por segmento',
        'data_quality': quality,
        'summary': {
            'ordenes': len(orders),
            'defectos': _round(total_defects),
            'produccion': _round(total_produced),
            'tasa_defecto': _round(overall_rate) if overall_rate is not None else None,
            'tasa_defecto_prevista': _round(trend_forecast[-1]) if trend_forecast else None,
            'causas_recurrentes': sum(1 for cause in causes if cause['recurrencia_periodos'] >= 2),
            'proveedores_riesgo': sum(1 for item in supplier_risks if item['status'] in {'rojo', 'amarillo'}),
        },
        'trend': [
            {'label': week.strftime('%d/%m'), 'value': _round(rate_for(weekly_total[week]))}
            for week in week_starts
            if rate_for(weekly_total[week]) is not None
        ],
        'trend_forecast': _round(trend_forecast[-1]) if trend_forecast else None,
        'trend_confidence': trend_evaluation['confidence'],
        'trend_confidence_label': trend_confidence_label,
        'trend_confidence_status': trend_confidence_status,
        'trend_reliable': len(trend_values) >= min_samples,
        'segment_predictions': segments[:limit],
        'supplier_risks': supplier_risks[:limit],
        'material_contributors': material_contributors[:limit],
        'contributors': [
            *[
                {
                    'tipo': 'PROCESO',
                    'nombre': cause['causa'],
                    'contribucion': cause['porcentaje'],
                    'severidad': cause['severidad'],
                    'accion': cause['accion'],
                }
                for cause in causes[:limit]
            ],
            *[
                {
                    'tipo': 'MATERIAL',
                    'nombre': f"{item['segmento']} · {item['nombre']}",
                    'contribucion': item['probabilidad_defecto'],
                    'severidad': 'Alta' if item['status'] == 'rojo' else ('Media' if item['status'] == 'amarillo' else 'Baja'),
                    'accion': item['accion'],
                }
                for item in material_contributors[:limit]
            ],
        ][:limit],
        'causes': causes[:limit],
        'alerts': alerts[:limit],
        'claim_risk': {
            'claims': claim_count,
            'orders': len(orders),
            'probability': _round(claim_probability) if claim_probability is not None else None,
            'status': claim_risk_status,
            'confidence_label': claim_label,
            'confidence_status': claim_status,
            'reliable': len(orders) >= min_samples,
        },
    }


def registrar_predicciones_calidad(forecast: dict, usuario=None) -> int:
    """Registra el pronóstico de calidad por segmento en el contrato común."""
    if not forecast.get('enabled'):
        return 0
    modelo = 'Tendencia de defectos por segmento'
    catalog = {
        'probabilidad_defecto': ('Probabilidad de defecto', '%', False),
        'tendencia_scrap': ('Tendencia de scrap', '%', False),
        'riesgo_proveedor': ('Riesgo de proveedor', '%', False),
        'riesgo_material': ('Riesgo de material', '%', False),
        'riesgo_reclamo': ('Riesgo de reclamo', '%', False),
        'reincidencia_defecto': ('Reincidencia de defecto', '%', False),
    }
    target_date = forecast['fecha_fin'] + timedelta(weeks=forecast['horizon_weeks'])
    registros = 0

    def register(code, alcance, segmento, label, current, predicted, confidence, status, action, metadata):
        nonlocal registros
        name, unit, higher_is_better = catalog[code]
        CatalogoMetricaPredictiva.objects.update_or_create(
            departamento='QA',
            codigo=code,
            defaults={
                'nombre': name,
                'unidad': unit,
                'mayor_es_mejor': higher_is_better,
                'modelo_recomendado': modelo,
                'accion_default': action,
                'activo': True,
            },
        )
        RegistroPrediccion.objects.update_or_create(
            departamento='QA',
            alcance=alcance,
            segmento=segmento,
            codigo_metrica=code,
            periodo_inicio=forecast['fecha_inicio'],
            periodo_fin=forecast['fecha_fin'],
            fecha_objetivo=target_date,
            horizonte=forecast['horizon_weeks'],
            modelo=modelo,
                version_modelo=VERSION_MODELO_ACTUAL,
            defaults={
                'nombre_metrica': f'{label} · {segmento}',
                'valor_actual': current,
                'valor_predicho': predicted,
                'limite_inferior': None,
                'limite_superior': None,
                'confianza': confidence,
                'estado': status,
                'accion_recomendada': action,
                'creado_por': usuario,
                'metadatos': metadata,
            },
        )
        registros += 1

    for segment in forecast.get('segment_predictions', []):
        code = 'riesgo_proveedor' if segment['dimension'] == 'PROVEEDOR' else 'probabilidad_defecto'
        register(
            code,
            segment['dimension'],
            segment['segmento'],
            segment['dimension'].title(),
            segment['tasa_actual'],
            segment['probabilidad_defecto'],
            segment['confianza'],
            segment['display_status'],
            segment['accion'],
            {
                'muestras': segment['muestras'],
                'backtests': segment['backtests'],
                'mape': _float_or_none(segment['mape']),
                'mae': _float_or_none(segment['mae']),
                'causa_probable': segment['causa_probable'],
                'tendencia': segment['tendencia'],
            },
        )

    for material in forecast.get('material_contributors', []):
        register(
            'riesgo_material', 'MATERIAL', material['segmento'], material.get('nombre') or material['segmento'],
            material['probabilidad_defecto'], material['probabilidad_defecto'], material['confianza'],
            material['display_status'], material['accion'],
            {'muestras': material['muestras'], 'confianza': _float_or_none(material['confianza'])},
        )

    trend_current = forecast['trend'][-1]['value'] if forecast.get('trend') else None
    register(
        'tendencia_scrap', 'GLOBAL', 'GLOBAL', 'Scrap global', trend_current, forecast.get('trend_forecast'),
        forecast.get('trend_confidence'), forecast.get('trend_confidence_status'),
        'Revisar causas recurrentes y confirmar acciones correctivas.',
        {'muestras': len(forecast.get('trend', [])), 'confiable': forecast.get('trend_reliable', False)},
    )
    claim = forecast.get('claim_risk') or {}
    register(
        'riesgo_reclamo', 'GLOBAL', 'GLOBAL', 'Reclamo de cliente', None, claim.get('probability'),
        Decimal('70') if claim.get('reliable') else Decimal('40'), claim.get('status', 'amarillo'),
        'Revisar lotes liberados y reclamos abiertos.',
        {'reclamos': claim.get('claims', 0), 'ordenes': claim.get('orders', 0)},
    )
    for cause in forecast.get('causes', []):
        register(
            'reincidencia_defecto', 'CAUSA', cause['causa'], 'Reincidencia',
            Decimal(cause['recurrencia_periodos']), Decimal(cause['recurrencia_periodos']),
            Decimal('70') if cause['recurrencia_periodos'] >= 4 else Decimal('40'),
            'rojo' if cause['severidad'] == 'Alta' else 'amarillo', cause['accion'],
            {'ocurrencias': cause['ocurrencias'], 'porcentaje': _float_or_none(cause['porcentaje'])},
        )
    return registros


def calcular_predicciones_mantenimiento(days: int = 90, horizon_weeks: int = 4,
                                         limit: int = 10, min_samples: int = 4) -> dict:
    """Calcula riesgo de paro por máquina usando bitácora, uso y calidad."""
    end_date = timezone.localdate()
    start_date = end_date - timedelta(days=max(days - 1, 1))
    machines = list(CostoHoraMaquina.objects.filter(activo=True).order_by('linea_produccion', 'maquina_nombre'))
    stops = list(
        RegistroParoMantenimiento.objects
        .filter(fecha_paro__range=(start_date, end_date))
        .select_related('maquina', 'orden')
    )
    usages = list(
        RegistroUsoRecursoProduccion.objects
        .filter(
            tipo_recurso=RegistroUsoRecursoProduccion.TipoRecurso.MAQUINA,
            fecha_creacion__date__range=(start_date, end_date),
        )
        .select_related('costo_maquina', 'orden')
    )
    qa_reports = list(
        InformeValidacionDefectoQA.objects
        .filter(
            falla_maquina=True,
            defecto__fecha_creacion__date__range=(start_date, end_date),
        )
        .select_related('defecto', 'defecto__orden')
    )

    first_week = start_date - timedelta(days=start_date.weekday())
    last_week = end_date - timedelta(days=end_date.weekday())
    week_starts = []
    cursor = first_week
    while cursor <= last_week:
        week_starts.append(cursor)
        cursor += timedelta(days=7)

    stops_by_machine = defaultdict(list)
    stop_hours_by_week = defaultdict(lambda: defaultdict(Decimal))
    severity_weight = {'BAJA': Decimal('0'), 'MEDIA': Decimal('1'), 'ALTA': Decimal('2'), 'CRITICA': Decimal('3')}
    for stop in stops:
        stops_by_machine[stop.maquina_id].append(stop)
        week = stop.fecha_paro - timedelta(days=stop.fecha_paro.weekday())
        stop_hours_by_week[stop.maquina_id][week] += _to_decimal(stop.horas_paro)

    usage_by_machine = defaultdict(lambda: {'hours': ZERO, 'cost': ZERO, 'produced': ZERO, 'orders': set()})
    for usage in usages:
        bucket = usage_by_machine[usage.costo_maquina_id]
        bucket['hours'] += _to_decimal(usage.horas_reales)
        bucket['cost'] += _to_decimal(usage.costo_total)
        if usage.orden_id:
            bucket['orders'].add(usage.orden_id)
            bucket['produced'] += _to_decimal(usage.orden.cantidad_producida)

    machine_by_line = defaultdict(list)
    for machine in machines:
        machine_by_line[machine.linea_produccion].append(machine.id)
    defects_by_machine = defaultdict(Decimal)
    for report in qa_reports:
        order = report.defecto.orden
        if order and order.linea_produccion:
            for machine_id in machine_by_line.get(order.linea_produccion, []):
                defects_by_machine[machine_id] += _to_decimal(report.defecto.cantidad_defectos)

    risks = []
    for machine in machines:
        machine_stops = stops_by_machine.get(machine.id, [])
        values = [_to_decimal(stop_hours_by_week[machine.id].get(week, ZERO)) for week in week_starts]
        evaluation = _evaluate_forecast(values, horizon_weeks) if values else {'confidence': ZERO, 'mae': ZERO, 'mape': None, 'backtests': 0}
        forecast_values, _, trend = _linear_forecast(values, steps=horizon_weeks) if values else ([], ZERO, 'sin datos')
        if len(values) < 3 and values:
            forecast_values = [_weighted_average(values) for _ in range(horizon_weeks)]
        forecast_values = [max(ZERO, _round(value)) for value in forecast_values]
        confidence_label, confidence_status = _confidence_label(len(values), evaluation['confidence'])
        usage = usage_by_machine[machine.id]
        produced = usage['produced']
        cycle_hours = _safe_div(usage['hours'], produced) if produced else None
        critical_hours = sum(
            (_to_decimal(stop.horas_paro) for stop in machine_stops if stop.severidad == RegistroParoMantenimiento.Severidad.CRITICA),
            ZERO,
        )
        projected_hours = sum(forecast_values, ZERO)
        defect_count = defects_by_machine[machine.id]
        risk_score = _bounded_percent(
            projected_hours * Decimal('8')
            + critical_hours * Decimal('4')
            + defect_count * Decimal('2')
        )
        if risk_score >= Decimal('70'):
            status = 'rojo'
            action = 'Programar mantenimiento inmediato y validar la causa del paro antes de liberar la siguiente orden.'
            revision_days = 1
        elif risk_score >= Decimal('35'):
            status = 'amarillo'
            action = 'Inspeccionar el equipo, revisar paros recurrentes y confirmar el plan preventivo.'
            revision_days = 14
        else:
            status = 'verde'
            action = 'Mantener el plan preventivo y continuar registrando horas, paros y causas.'
            revision_days = 30
        if not machine_stops:
            action = 'No hay paros históricos de esta máquina; registrar la bitácora para mejorar la predicción.'
        if confidence_status == 'rojo':
            display_status = 'amarillo' if status == 'verde' else status
        else:
            display_status = status
        risks.append({
            'maquina_id': machine.id,
            'maquina': machine.maquina_nombre,
            'linea': machine.linea_produccion,
            'costo_hora': _round(machine.costo_hora),
            'horas_paro': _round(sum((_to_decimal(stop.horas_paro) for stop in machine_stops), ZERO)),
            'paros': len(machine_stops),
            'horas_maquina': _round(usage['hours']),
            'costo_maquina': _round(usage['cost']),
            'ordenes': len(usage['orders']),
            'produccion': _round(produced),
            'horas_ciclo_unidad': _round(cycle_hours) if cycle_hours is not None else None,
            'defectos_maquina': _round(defect_count),
            'horas_paro_previstas': _round(projected_hours),
            'tendencia': trend,
            'riesgo_score': _round(risk_score, '0.1'),
            'status': status,
            'display_status': display_status,
            'confianza': evaluation['confidence'],
            'confianza_label': confidence_label,
            'confianza_status': confidence_status,
            'muestras': len(values),
            'backtests': evaluation['backtests'],
            'mae': evaluation['mae'],
            'mape': evaluation['mape'],
            'forecast_reliable': bool(machine_stops) and len(values) >= min_samples,
            'fecha_revision': end_date + timedelta(days=revision_days),
            'accion': action,
        })

    risks.sort(key=lambda item: (-item['riesgo_score'], item['maquina']))
    total_stops = sum((_to_decimal(stop.horas_paro) for stop in stops), ZERO)
    total_machine_hours = sum((item['horas_maquina'] for item in risks), ZERO)
    total_machine_cost = sum((item['costo_maquina'] for item in risks), ZERO)
    quality = 'alta' if len(stops) >= 8 and len(week_starts) >= 8 else ('media' if stops or usages else 'baja')
    alerts = [
        {
            'status': item['display_status'],
            'label': f"{item['linea']} · {item['maquina']}",
            'value': f"{item['riesgo_score']}%",
            'message': item['accion'],
        }
        for item in risks if item['status'] in {'rojo', 'amarillo'}
    ][:limit]
    return {
        'enabled': bool(machines),
        'fecha_inicio': start_date,
        'fecha_fin': end_date,
        'days': days,
        'horizon_weeks': horizon_weeks,
        'min_samples': min_samples,
        'method': 'Tendencia de paros, uso de máquina y defectos asociados',
        'data_quality': quality,
        'summary': {
            'maquinas': len(machines),
            'riesgo_alto': sum(1 for item in risks if item['status'] in {'rojo', 'amarillo'}),
            'horas_paro': _round(total_stops),
            'horas_maquina': _round(total_machine_hours),
            'costo_maquina': _round(total_machine_cost),
            'defectos_maquina': _round(sum(defects_by_machine.values(), ZERO)),
            'proxima_revision': min((item['fecha_revision'] for item in risks), default=None),
        },
        'risks': risks[:limit],
        'alerts': alerts,
        'trend': [
            {'label': week.strftime('%d/%m'), 'value': _round(sum(stop_hours_by_week[machine.id].get(week, ZERO) for machine in machines))}
            for week in week_starts
        ],
        'trend_forecast': _round(sum((item['horas_paro_previstas'] for item in risks), ZERO)),
        'trend_reliable': bool(stops) and len(week_starts) >= min_samples,
    }


def registrar_predicciones_mantenimiento(forecast: dict, usuario=None) -> int:
    """Persiste el riesgo de mantenimiento en el catálogo predictivo común."""
    if not forecast.get('enabled'):
        return 0
    modelo = 'Tendencia de paros y uso de máquina'
    catalog = {
        'riesgo_mantenimiento': ('Riesgo de mantenimiento', '%', False),
        'paros_previstos': ('Horas de paro previstas', 'h', False),
        'cambio_ciclo': ('Horas de ciclo por unidad', 'h/unidad', False),
        'defectos_maquina': ('Defectos asociados a máquina', 'piezas', False),
    }
    registros = 0
    target_date = forecast['fecha_fin'] + timedelta(weeks=forecast['horizon_weeks'])

    for item in forecast.get('risks', []):
        metadata = {
            'maquina_id': item['maquina_id'],
            'linea': item['linea'],
            'paros': item['paros'],
            'muestras': item['muestras'],
            'backtests': item['backtests'],
            'mae': _float_or_none(item['mae']),
            'mape': _float_or_none(item['mape']),
            'confiable': item['forecast_reliable'],
        }
        metrics = [
            ('riesgo_mantenimiento', item['riesgo_score'], item['riesgo_score'], item['display_status'], item['accion']),
            ('paros_previstos', item['horas_paro'], item['horas_paro_previstas'], item['display_status'], item['accion']),
            ('cambio_ciclo', item['horas_ciclo_unidad'], item['horas_ciclo_unidad'], item['display_status'], 'Revisar cambios de ciclo y validar la capacidad real del equipo.'),
            ('defectos_maquina', item['defectos_maquina'], item['defectos_maquina'], item['display_status'], 'Comparar defectos con paros y ejecutar acción correctiva si se repite la causa.'),
        ]
        for code, current, predicted, status, action in metrics:
            name, unit, higher_is_better = catalog[code]
            CatalogoMetricaPredictiva.objects.update_or_create(
                departamento='Mantenimiento', codigo=code,
                defaults={
                    'nombre': name,
                    'unidad': unit,
                    'mayor_es_mejor': higher_is_better,
                    'modelo_recomendado': modelo,
                    'accion_default': action,
                    'activo': True,
                },
            )
            RegistroPrediccion.objects.update_or_create(
                departamento='Mantenimiento', alcance='MAQUINA', segmento=item['maquina'],
                codigo_metrica=code, periodo_inicio=forecast['fecha_inicio'],
                periodo_fin=forecast['fecha_fin'], fecha_objetivo=item['fecha_revision'],
                horizonte=forecast['horizon_weeks'], modelo=modelo, version_modelo=VERSION_MODELO_ACTUAL,
                defaults={
                    'nombre_metrica': f'{name} · {item["maquina"]}',
                    'valor_actual': current,
                    'valor_predicho': predicted,
                    'limite_inferior': None,
                    'limite_superior': None,
                    'confianza': item['confianza'],
                    'estado': status,
                    'accion_recomendada': action,
                    'creado_por': usuario,
                    'metadatos': metadata,
                },
            )
            registros += 1
    return registros
