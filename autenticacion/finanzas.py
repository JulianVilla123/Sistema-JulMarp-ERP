from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO

from django.db.models import Prefetch, Q, Sum
from django.utils import timezone

from .kpi_produccion import calcular_kpis_produccion
from .models import (
    CosteoProduccion,
    CuentaPorPagarCobrar,
    DeclaracionImpuesto,
    EstadoFinanciero,
    InventarioAlmacen,
    LoteProduccion,
    OrdenCompra,
    OrdenFabricacion,
    OrdenFabricacionDetalle,
    PresupuestoFinanciero,
    ProveedorMaterialPrecio,
    RegistroUsoRecursoProduccion,
    ReporteFinanciero,
)

ZERO = Decimal('0')
ONE = Decimal('1')
HUNDRED = Decimal('100')


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


def _get_period_bounds(fecha_inicio: date | None = None, fecha_fin: date | None = None) -> tuple[date, date]:
    end_date = fecha_fin or timezone.localdate()
    start_date = fecha_inicio or (end_date - timedelta(days=29))
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    return start_date, end_date


def _latest_material_costs(material_ids: set[int]) -> dict[int, Decimal]:
    cost_map: dict[int, Decimal] = {}
    if not material_ids:
        return cost_map

    precios = (
        ProveedorMaterialPrecio.objects
        .filter(material_id__in=material_ids)
        .order_by('material_id', '-fecha_actualizacion')
    )
    for precio in precios:
        if precio.material_id not in cost_map:
            cost_map[precio.material_id] = _to_decimal(precio.precio_unitario)
    return cost_map


def _orders_in_period(fecha_inicio: date, fecha_fin: date):
    filtros = (
        Q(fecha_creacion__date__range=(fecha_inicio, fecha_fin)) |
        Q(fecha_actualizacion__date__range=(fecha_inicio, fecha_fin)) |
        Q(fecha_fin_real__date__range=(fecha_inicio, fecha_fin))
    )
    return (
        OrdenFabricacion.objects
        .filter(filtros)
        .select_related('bom', 'plan')
        .prefetch_related(
            Prefetch('detalles', queryset=OrdenFabricacionDetalle.objects.select_related('material')),
            'lotes_produccion',
            'usos_recursos',
        )
        .distinct()
    )


def _lots_in_period(fecha_inicio: date, fecha_fin: date):
    return (
        LoteProduccion.objects
        .filter(
            Q(fecha_captura__range=(fecha_inicio, fecha_fin)) |
            Q(fecha_actualizacion__date__range=(fecha_inicio, fecha_fin))
        )
        .select_related('bom', 'orden_fabricacion')
        .distinct()
    )


def consolidar_costeos_produccion(usuario=None, fecha_inicio: date | None = None, fecha_fin: date | None = None) -> list[CosteoProduccion]:
    start_date, end_date = _get_period_bounds(fecha_inicio, fecha_fin)
    orders = list(_orders_in_period(start_date, end_date))
    lotes = list(_lots_in_period(start_date, end_date))
    lotes_por_orden = {}
    for lote in lotes:
        if lote.orden_fabricacion_id:
            lotes_por_orden.setdefault(lote.orden_fabricacion_id, []).append(lote)

    material_ids = {
        detalle.material_id
        for order in orders
        for detalle in order.detalles.all()
    }
    material_costs = _latest_material_costs(material_ids)
    registros = []

    for order in orders:
        costo_material_plan = ZERO
        costo_material_real = ZERO
        for detalle in order.detalles.all():
            costo_unitario = material_costs.get(detalle.material_id, ZERO)
            costo_material_plan += _to_decimal(detalle.cantidad_requerida) * costo_unitario
            costo_material_real += _to_decimal(detalle.cantidad_consumida) * costo_unitario

        usos = list(order.usos_recursos.all())
        costo_maquina_real = sum(
            (_to_decimal(uso.costo_total) for uso in usos if uso.tipo_recurso == RegistroUsoRecursoProduccion.TipoRecurso.MAQUINA),
            ZERO,
        )
        costo_operador_real = sum(
            (_to_decimal(uso.costo_total) for uso in usos if uso.tipo_recurso == RegistroUsoRecursoProduccion.TipoRecurso.OPERADOR),
            ZERO,
        )
        costo_total_plan = costo_material_plan
        costo_total_real = costo_material_real + costo_maquina_real + costo_operador_real
        ingreso_estimado = _to_decimal(order.cantidad_producida) * Decimal('12.50')
        rentabilidad = ingreso_estimado - costo_total_real
        margen_pct = _safe_div(rentabilidad, ingreso_estimado) * HUNDRED if ingreso_estimado > 0 else ZERO

        defaults = {
            'lote_produccion': None,
            'costo_material_plan': _round(costo_material_plan),
            'costo_material_real': _round(costo_material_real),
            'costo_maquina_real': _round(costo_maquina_real),
            'costo_operador_real': _round(costo_operador_real),
            'costo_total_plan': _round(costo_total_plan),
            'costo_total_real': _round(costo_total_real),
            'ingreso_estimado': _round(ingreso_estimado),
            'rentabilidad': _round(rentabilidad),
            'margen_pct': _round(margen_pct),
            'estado': CosteoProduccion.EstadoCosteo.CERRADO,
            'detalle': {
                'producto': order.bom.producto,
                'cantidad_planificada': float(_to_decimal(order.cantidad_planificada)),
                'cantidad_producida': float(_to_decimal(order.cantidad_producida)),
                'lotes_relacionados': [lote.folio for lote in lotes_por_orden.get(order.id, [])],
            },
            'creado_por': usuario or order.creado_por,
            'actualizado_por': usuario or order.creado_por,
        }
        costeo, _ = CosteoProduccion.objects.update_or_create(
            orden_fabricacion=order,
            lote_produccion=None,
            defaults=defaults,
        )
        registros.append(costeo)

        lotes_orden = lotes_por_orden.get(order.id, [])
        total_lote_qty = sum((_to_decimal(lote.cantidad_producida) for lote in lotes_orden), ZERO)
        for lote in lotes_orden:
            proporcion = _safe_div(_to_decimal(lote.cantidad_producida), total_lote_qty) if total_lote_qty > 0 else ZERO
            lote_defaults = {
                'orden_fabricacion': order,
                'costo_material_plan': _round(costo_material_plan * proporcion),
                'costo_material_real': _round(costo_material_real * proporcion),
                'costo_maquina_real': _round(costo_maquina_real * proporcion),
                'costo_operador_real': _round(costo_operador_real * proporcion),
                'costo_total_plan': _round(costo_total_plan * proporcion),
                'costo_total_real': _round(costo_total_real * proporcion),
                'ingreso_estimado': _round(ingreso_estimado * proporcion),
                'rentabilidad': _round(rentabilidad * proporcion),
                'margen_pct': _round(margen_pct),
                'estado': CosteoProduccion.EstadoCosteo.CERRADO,
                'detalle': {
                    'producto': lote.bom.producto,
                    'cantidad_producida': float(_to_decimal(lote.cantidad_producida)),
                    'cliente_destino': lote.cliente_destino.nombre if lote.cliente_destino_id else '',
                },
                'creado_por': usuario or lote.creado_por,
                'actualizado_por': usuario or lote.creado_por,
            }
            lote_costeo, _ = CosteoProduccion.objects.update_or_create(
                lote_produccion=lote,
                defaults=lote_defaults,
            )
            registros.append(lote_costeo)

    return registros


def calcular_dashboard_finanzas(fecha_inicio: date | None = None, fecha_fin: date | None = None) -> dict:
    start_date, end_date = _get_period_bounds(fecha_inicio, fecha_fin)
    cuentas = list(
        CuentaPorPagarCobrar.objects.filter(fecha_emision__range=(start_date, end_date)).select_related('orden_compra', 'proveedor', 'cliente_compra')
    )
    # Solo se consideran presupuestos activos cuyo periodo se cruza con el
    # filtro seleccionado; un presupuesto fuera de rango no debe pintar la
    # gráfica del dashboard actual.
    presupuestos = list(
        PresupuestoFinanciero.objects
        .filter(fecha_inicio__lte=end_date, fecha_fin__gte=start_date, activo=True)
        .order_by('-fecha_fin', 'nombre')
    )
    ordenes_compra = list(OrdenCompra.objects.filter(fecha_orden__range=(start_date, end_date)).select_related('proveedor'))
    kpis_mfg = calcular_kpis_produccion(fecha_inicio=start_date, fecha_fin=end_date)
    costeos = list(CosteoProduccion.objects.filter(fecha_actualizacion__date__range=(start_date, end_date), lote_produccion__isnull=True).select_related('orden_fabricacion', 'orden_fabricacion__bom'))
    declaraciones = list(DeclaracionImpuesto.objects.filter(periodo_inicio__lte=end_date, periodo_fin__gte=start_date))

    ingresos = sum((_to_decimal(c.monto_pagado) for c in cuentas if c.tipo == CuentaPorPagarCobrar.TipoCuenta.POR_COBRAR), ZERO)
    egresos = sum((_to_decimal(c.monto_pagado) for c in cuentas if c.tipo == CuentaPorPagarCobrar.TipoCuenta.POR_PAGAR), ZERO)
    ventas_total = sum((_to_decimal(c.monto_total) for c in cuentas if c.tipo == CuentaPorPagarCobrar.TipoCuenta.POR_COBRAR), ZERO)
    flujo_caja = ingresos - egresos

    presupuesto_total = sum((_to_decimal(p.monto_presupuestado) for p in presupuestos), ZERO)
    gasto_real = sum((_to_decimal(p.monto_real) for p in presupuestos if p.categoria in {PresupuestoFinanciero.Categoria.GASTO, PresupuestoFinanciero.Categoria.OPEX, PresupuestoFinanciero.Categoria.CAPEX}), ZERO)
    ingreso_presupuestado = sum((_to_decimal(p.monto_presupuestado) for p in presupuestos if p.categoria == PresupuestoFinanciero.Categoria.INGRESO), ZERO)
    rentabilidad_total = sum((_to_decimal(c.rentabilidad) for c in costeos), ZERO)
    ingreso_estimado_total = sum((_to_decimal(c.ingreso_estimado) for c in costeos), ZERO)
    rentabilidad_pct = _safe_div(rentabilidad_total, ingreso_estimado_total) * HUNDRED if ingreso_estimado_total > 0 else ZERO
    presupuesto_utilizacion = _safe_div(gasto_real, presupuesto_total) * HUNDRED if presupuesto_total > ZERO else ZERO

    oc_pendientes = sum(1 for oc in ordenes_compra if oc.estado in {OrdenCompra.EstadoOrden.BORRADOR, OrdenCompra.EstadoOrden.APROBADA, OrdenCompra.EstadoOrden.ENVIADA, OrdenCompra.EstadoOrden.PARCIAL})
    oc_completadas = sum(1 for oc in ordenes_compra if oc.estado == OrdenCompra.EstadoOrden.RECIBIDA)
    impuestos_pendientes = sum(1 for item in declaraciones if item.estado != DeclaracionImpuesto.EstadoDeclaracion.PRESENTADA)

    alerts = []
    if flujo_caja < 0:
        alerts.append('Flujo de caja en negativo en el periodo seleccionado.')
    if presupuesto_total > 0 and gasto_real > presupuesto_total:
        alerts.append('El gasto real supera el presupuesto total activo.')
    if oc_pendientes > oc_completadas and oc_pendientes > 0:
        alerts.append('Hay más órdenes de compra pendientes que completadas.')
    if impuestos_pendientes > 0:
        alerts.append(f'Existen {impuestos_pendientes} declaraciones fiscales pendientes.')

    presupuesto_has_data = any(
        _to_decimal(p.monto_presupuestado) > ZERO or _to_decimal(p.monto_real) > ZERO
        for p in presupuestos
    )
    costo_planificado = _to_decimal(kpis_mfg['cost']['costo_planificado'])
    costo_real = _to_decimal(kpis_mfg['cost']['costo_real'])
    variacion_costos = _to_decimal(kpis_mfg['cost']['variacion_costos'])
    # El costo total de producción también puede incluir unidades fabricadas
    # que todavía no se han vendido. Para medir rentabilidad de ventas no se
    # debe cargar todo ese importe contra las ventas del periodo. Mientras el
    # ERP no tenga líneas de venta vinculadas a lote/producto, se estima el
    # costo de ventas con la proporción ventas / ingreso estimado producido.
    proporcion_vendida = (
        min(ONE, _safe_div(ventas_total, ingreso_estimado_total))
        if ingreso_estimado_total > ZERO
        else ZERO
    )
    costo_ventas_estimado = costo_real * proporcion_vendida if costo_real > ZERO else ZERO
    costo_produccion_no_vendido = max(ZERO, costo_real - costo_ventas_estimado)
    utilidad_ventas_costos = ventas_total - costo_ventas_estimado
    margen_ventas_costos = _safe_div(utilidad_ventas_costos, ventas_total) * HUNDRED if ventas_total > ZERO else ZERO
    variacion_costos_pct = _safe_div(variacion_costos, costo_planificado) * HUNDRED if costo_planificado > ZERO else ZERO
    costos_has_data = costo_planificado > ZERO or costo_real > ZERO
    ventas_costos_has_data = ventas_total > ZERO or costo_real > ZERO
    oc_has_data = oc_pendientes > 0 or oc_completadas > 0

    # Valuación del inventario disponible: existencias por almacén por el
    # último costo unitario vigente registrado para cada material. Agrupar por
    # material evita duplicar el stock cuando existe en varios almacenes o
    # lotes.
    stock_rows = list(
        InventarioAlmacen.objects
        .filter(stock_actual__gt=ZERO)
        .values('material_id', 'material__sku', 'material__nombre', 'material__um')
        .annotate(stock_total=Sum('stock_actual'))
        .order_by('-stock_total')
    )
    stock_price_map = _latest_material_costs({row['material_id'] for row in stock_rows})
    inventario_unidades = ZERO
    inventario_valor = ZERO
    inventario_materiales_valorados = 0
    inventario_materiales_sin_costo = 0
    inventario_detalle = []
    for row in stock_rows:
        stock_total = _to_decimal(row['stock_total'])
        costo_unitario = _to_decimal(stock_price_map.get(row['material_id']))
        valor_stock = stock_total * costo_unitario
        inventario_unidades += stock_total
        inventario_valor += valor_stock
        if costo_unitario > ZERO:
            inventario_materiales_valorados += 1
        else:
            inventario_materiales_sin_costo += 1
        inventario_detalle.append({
            'sku': row['material__sku'],
            'nombre': row['material__nombre'],
            'um': row['material__um'],
            'stock': _round(stock_total),
            'costo_unitario': _round(costo_unitario),
            'valor': _round(valor_stock),
        })
    inventario_detalle.sort(key=lambda item: item['valor'], reverse=True)
    inventario_has_data = bool(stock_rows)

    return {
        'fecha_inicio': start_date,
        'fecha_fin': end_date,
        'kpis': {
            'flujo_caja': _round(flujo_caja),
            'ventas_total': _round(ventas_total),
            'costo_ventas_estimado': _round(costo_ventas_estimado),
            'costo_produccion_no_vendido': _round(costo_produccion_no_vendido),
            'utilidad_ventas_costos': _round(utilidad_ventas_costos),
            'margen_ventas_costos': _round(margen_ventas_costos),
            'rentabilidad': _round(rentabilidad_total),
            'rentabilidad_pct': _round(rentabilidad_pct),
            'presupuesto_total': _round(presupuesto_total),
            'gasto_real': _round(gasto_real),
            'ingreso_presupuestado': _round(ingreso_presupuestado),
            'presupuesto_utilizacion': _round(presupuesto_utilizacion),
            'oc_pendientes': oc_pendientes,
            'oc_completadas': oc_completadas,
            'costeos_generados': len(costeos),
            'impuestos_pendientes': impuestos_pendientes,
        },
        'produccion': {
            'costo_planificado': _round(costo_planificado),
            'costo_real': _round(costo_real),
            'variacion_costos': _round(variacion_costos),
            'variacion_costos_pct': _round(variacion_costos_pct),
            'rentabilidad': _round(rentabilidad_total),
            'rentabilidad_pct': _round(rentabilidad_pct),
            'costos_has_data': costos_has_data,
            'rentabilidad_has_data': bool(costeos),
            'oee': kpis_mfg['oee']['oee'],
            'tasa_rechazo': kpis_mfg['reject']['tasa_rechazo'],
        },
        'charts': {
            'presupuesto_labels': ['Presupuesto', 'Gasto real', 'Ingreso presupuestado'],
            'presupuesto_values': [float(_round(presupuesto_total)), float(_round(gasto_real)), float(_round(ingreso_presupuestado))],
            'presupuesto_has_data': presupuesto_has_data,
            'presupuesto_count': len(presupuestos),
            'presupuesto_period': {
                'fecha_inicio': start_date.isoformat(),
                'fecha_fin': end_date.isoformat(),
            },
            'costos_labels': ['Costo planificado', 'Costo real'],
            'costos_values': [float(_round(costo_planificado)), float(_round(costo_real))],
            'costos_has_data': costos_has_data,
            'ventas_costos_labels': ['Ventas', 'Costo de ventas', 'Utilidad'],
            'ventas_costos_values': [
                float(_round(ventas_total)),
                float(_round(costo_ventas_estimado)),
                float(_round(utilidad_ventas_costos)),
            ],
            'ventas_costos_has_data': ventas_costos_has_data,
            'oc_labels': ['Pendientes', 'Completadas'],
            'oc_values': [oc_pendientes, oc_completadas],
            'oc_has_data': oc_has_data,
            'oc_count': oc_pendientes + oc_completadas,
        },
        'ordenes_compra_recientes': ordenes_compra[:8],
        'costeos_recientes': costeos[:8],
        'cuentas_recientes': cuentas[:8],
        'presupuestos': presupuestos[:8],
        'inventario': {
            'valor_stock': _round(inventario_valor),
            'unidades_stock': _round(inventario_unidades),
            'materiales_con_stock': len(stock_rows),
            'materiales_valorados': inventario_materiales_valorados,
            'materiales_sin_costo': inventario_materiales_sin_costo,
            'has_data': inventario_has_data,
            'detalle': inventario_detalle[:6],
        },
        'alertas': alerts,
    }


def generar_reporte_financiero(usuario=None, tipo: str = ReporteFinanciero.TipoReporte.KPI, fecha_inicio: date | None = None, fecha_fin: date | None = None) -> ReporteFinanciero:
    data = calcular_dashboard_finanzas(fecha_inicio=fecha_inicio, fecha_fin=fecha_fin)
    nombre = f'Reporte financiero {data["fecha_inicio"]} - {data["fecha_fin"]}'
    reporte = ReporteFinanciero.objects.create(
        nombre=nombre,
        tipo=tipo,
        fecha_inicio=data['fecha_inicio'],
        fecha_fin=data['fecha_fin'],
        indicadores={
            'kpis': {key: float(value) if isinstance(value, Decimal) else value for key, value in data['kpis'].items()},
            'produccion': {key: float(value) if isinstance(value, Decimal) else value for key, value in data['produccion'].items()},
            'charts': data['charts'],
        },
        alertas=data['alertas'],
        creado_por=usuario,
        actualizado_por=usuario,
    )
    return reporte


def exportar_reporte_financiero_excel(reporte: ReporteFinanciero) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    indicadores = reporte.indicadores or {}
    kpis = indicadores.get('kpis', {})
    produccion = indicadores.get('produccion', {})

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Finanzas'
    sheet['A1'] = 'Reporte Financiero'
    sheet['A1'].font = Font(bold=True, size=14)
    sheet['A2'] = f'Periodo: {reporte.fecha_inicio} a {reporte.fecha_fin}'
    sheet.append([])
    sheet.append(['KPI', 'Valor'])
    for cell in sheet[4]:
        cell.font = Font(bold=True)
    for key, value in kpis.items():
        sheet.append([key.replace('_', ' ').title(), value])

    sheet.append([])
    sheet.append(['Indicadores producción', 'Valor'])
    for cell in sheet[sheet.max_row]:
        cell.font = Font(bold=True)
    for key, value in produccion.items():
        sheet.append([key.replace('_', ' ').title(), value])

    sheet.append([])
    sheet.append(['Alertas'])
    for cell in sheet[sheet.max_row]:
        cell.font = Font(bold=True)
    for alerta in reporte.alertas or ['Sin alertas críticas']:
        sheet.append([alerta])

    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def exportar_reporte_financiero_pdf(reporte: ReporteFinanciero) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    indicadores = reporte.indicadores or {}
    kpis = indicadores.get('kpis', {})
    produccion = indicadores.get('produccion', {})

    output = BytesIO()
    document = SimpleDocTemplate(output, pagesize=A4)
    styles = getSampleStyleSheet()
    story = [
        Paragraph('Reporte Financiero', styles['Title']),
        Paragraph(f'Periodo: {reporte.fecha_inicio} a {reporte.fecha_fin}', styles['BodyText']),
        Spacer(1, 12),
    ]

    table = Table(
        [['KPI', 'Valor']] + [[key.replace('_', ' ').title(), value] for key, value in kpis.items()],
        repeatRows=1,
    )
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1f3e67')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#dce7f5')),
        ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#f8fbff')),
    ]))
    story.append(table)
    story.append(Spacer(1, 12))

    prod_table = Table(
        [['Indicador producción', 'Valor']] + [[key.replace('_', ' ').title(), value] for key, value in produccion.items()],
        repeatRows=1,
    )
    prod_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#3f6b96')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#dce7f5')),
        ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#f8fbff')),
    ]))
    story.append(prod_table)
    story.append(Spacer(1, 12))
    story.append(Paragraph('Alertas', styles['Heading2']))
    for alerta in reporte.alertas or ['Sin alertas críticas']:
        story.append(Paragraph(f'- {alerta}', styles['BodyText']))

    document.build(story)
    return output.getvalue()


def generar_estado_financiero(usuario=None, fecha_inicio: date | None = None, fecha_fin: date | None = None, tipo: str = EstadoFinanciero.TipoEstado.RESULTADOS) -> EstadoFinanciero:
    data = calcular_dashboard_finanzas(fecha_inicio=fecha_inicio, fecha_fin=fecha_fin)
    kpis = data['kpis']
    nombre = f'{tipo.title()} {data["fecha_inicio"]} - {data["fecha_fin"]}'
    estado = EstadoFinanciero.objects.create(
        nombre=nombre,
        tipo=tipo,
        fecha_inicio=data['fecha_inicio'],
        fecha_fin=data['fecha_fin'],
        datos={
            'flujo_caja': float(kpis['flujo_caja']),
            'rentabilidad': float(kpis['rentabilidad']),
            'presupuesto_total': float(kpis['presupuesto_total']),
            'gasto_real': float(kpis['gasto_real']),
        },
        creado_por=usuario,
        actualizado_por=usuario,
    )
    return estado
