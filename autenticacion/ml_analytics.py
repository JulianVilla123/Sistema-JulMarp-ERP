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


def _linear_forecast(values: list[Decimal]) -> tuple[Decimal, Decimal, str]:
    clean_values = [_to_decimal(value) for value in values]
    if not clean_values:
        return ZERO, ZERO, 'Sin datos historicos suficientes.'
    if len(clean_values) == 1:
        return clean_values[0], ZERO, 'Prediccion basada en un solo punto historico.'

    n = Decimal(len(clean_values))
    xs = [Decimal(index) for index in range(len(clean_values))]
    mean_x = sum(xs, ZERO) / n
    mean_y = sum(clean_values, ZERO) / n
    numerator = sum(((x - mean_x) * (y - mean_y) for x, y in zip(xs, clean_values)), ZERO)
    denominator = sum(((x - mean_x) ** 2 for x in xs), ZERO)
    slope = _safe_div(numerator, denominator)
    intercept = mean_y - (slope * mean_x)
    next_x = Decimal(len(clean_values))
    prediction = intercept + (slope * next_x)

    residuals = [abs(y - (intercept + slope * x)) for x, y in zip(xs, clean_values)]
    mean_error = _safe_div(sum(residuals, ZERO), Decimal(len(residuals)))
    baseline = abs(mean_y) if mean_y else ONE
    confidence = max(Decimal('35'), min(Decimal('95'), Decimal('100') - (_safe_div(mean_error, baseline) * Decimal('100'))))
    detail = f"Regresion lineal con {len(clean_values)} periodos historicos."
    return prediction, confidence, detail


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


def calcular_predicciones_mfg(limit: int = 8) -> dict:
    reports = list(
        ReporteKPIProduccion.objects
        .order_by('-fecha_fin', '-fecha_generacion')[:limit]
    )
    reports.reverse()
    labels = [report.fecha_fin.strftime('%d/%m') for report in reports]
    next_label = 'Siguiente semana'

    metric_config = [
        ('oee', 'OEE esperado', '%', 'oee'),
        ('tasa_rechazo', 'Rechazo esperado', '%', 'tasa_rechazo'),
        ('cumplimiento_ordenes', 'Cumplimiento esperado', '%', 'cumplimiento_ordenes'),
        ('utilizacion_recursos', 'Uso recursos esperado', '%', 'utilizacion_recursos'),
    ]

    predictions = []
    trend_charts = []
    for field_name, label, unit, status_code in metric_config:
        values = [_to_decimal(getattr(report, field_name)) for report in reports]
        forecast, confidence, detail = _linear_forecast(values)
        if len(values) < 3:
            forecast = _weighted_average(values)
            confidence = Decimal('45') if values else ZERO
            detail = 'Promedio ponderado hasta reunir mas historial.'
        forecast = _bounded_percent(forecast)
        predictions.append({
            'code': field_name,
            'label': label,
            'value': _round(forecast),
            'unit': unit,
            'confidence': _round(confidence, '0.1'),
            'status': _status_for_metric(status_code, forecast),
            'detail': detail,
        })
        forecast_series = [None for _ in values]
        if values:
            forecast_series[-1] = _bounded_percent(values[-1])
        forecast_series.append(forecast)
        trend_charts.append({
            'code': field_name,
            'label': label,
            'unit': unit,
            'labels': labels + [next_label],
            'history': [_float_or_none(_bounded_percent(value)) for value in values] + [None],
            'forecast': [_float_or_none(value) for value in forecast_series],
            'confidence': _float_or_none(confidence),
            'status': _status_for_metric(status_code, forecast),
        })

    alerts = []
    for item in predictions:
        if item['status'] == 'rojo':
            alerts.append(f"ML anticipa riesgo en {item['label']}: {item['value']}{item['unit']}.")

    return {
        'enabled': bool(reports),
        'samples': len(reports),
        'method': 'Regresion lineal simple con respaldo de promedio ponderado',
        'predictions': predictions,
        'trend_charts': trend_charts,
        'alerts': alerts,
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
    total_planned_resource_cost = ZERO

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
        planned_resource_cost = real_cost
        scrap_rate = _safe_div(scrap_total, produced) * Decimal('100') if produced > 0 else ZERO

        total_produced += produced
        total_scrap += scrap_total
        total_input += material_input
        total_consumed += material_consumed
        total_real_cost += real_cost
        total_planned_resource_cost += planned_resource_cost

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
            'costo_planificado': _round(planned_resource_cost),
            'target_scrap_futuro': _round(scrap_total),
            'target_costo_futuro': _round(real_cost),
        })

    summary = {
        'piezas_producidas': _round(total_produced),
        'scrap': _round(total_scrap),
        'scrap_pct': _round(_safe_div(total_scrap, total_produced) * Decimal('100') if total_produced > 0 else ZERO),
        'entrada_materiales': _round(total_input),
        'consumo_materiales': _round(total_consumed),
        'diferencia_materiales': _round(total_input - total_consumed),
        'costo_real': _round(total_real_cost),
        'costo_planificado': _round(total_planned_resource_cost),
        'variacion_costos': _round(total_real_cost - total_planned_resource_cost),
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
        'targets': ['target_scrap_futuro', 'target_costo_futuro'],
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
