"""Variables de calendario y factores externos para la Fase 3."""

from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal

from .models import (
    FactorExternoPredictivo,
    OrdenCompra,
    OrdenCompraDetalle,
    ProveedorMaterialPrecio,
)


# En Inventario solo se aceptan factores que modifican directamente la
# demanda o el abastecimiento: estacionalidad y retrasos del proveedor.
INVENTORY_FACTOR_TYPE_CODES = frozenset({
    FactorExternoPredictivo.TipoFactor.TEMPORADA,
    FactorExternoPredictivo.TipoFactor.RETRASO_PROVEEDOR,
})


def variables_calendario(fecha):
    """Genera variables deterministas que todos los modelos pueden compartir."""
    if not isinstance(fecha, date):
        raise TypeError('fecha debe ser una instancia de date.')
    ultimo_dia = monthrange(fecha.year, fecha.month)[1]
    return {
        'anio': fecha.year,
        'mes': fecha.month,
        'semana_anio': fecha.isocalendar().week,
        'dia_semana': fecha.weekday(),
        'fin_de_mes': int(fecha.day == ultimo_dia),
        'inicio_de_mes': int(fecha.day == 1),
        'trimestre': ((fecha.month - 1) // 3) + 1,
    }


def factores_activos_en_periodo(*, desde, hasta, departamento=None, metrica=None):
    """Devuelve factores activos que se cruzan con el periodo solicitado."""
    factores = FactorExternoPredictivo.objects.filter(
        activo=True,
        fecha_inicio__lte=hasta,
        fecha_fin__gte=desde,
    )
    if departamento:
        factores = factores.filter(departamento__in=['Todos', departamento])
        if departamento == 'Inventario':
            factores = factores.filter(tipo__in=INVENTORY_FACTOR_TYPE_CODES)
    if metrica:
        factores = factores.filter(metrica__in=['GLOBAL', metrica])
    return factores


def impacto_factores_en_periodo(*, desde, hasta, departamento=None, metrica=None):
    """Resume el impacto registrado para enriquecer una semana de pronóstico."""
    factores = factores_activos_en_periodo(
        desde=desde,
        hasta=hasta,
        departamento=departamento,
        metrica=metrica,
    )
    return {
        'impacto_pct': sum((factor.impacto_pct for factor in factores), Decimal('0')),
        'factores': [
            {
                'tipo': factor.tipo,
                'nombre': factor.nombre,
                'departamento': factor.departamento,
                'metrica': factor.metrica,
                'impacto_pct': float(factor.impacto_pct),
                'fuente': factor.fuente,
            }
            for factor in factores
        ],
    }


def ajustar_valor_por_factores(
    valor,
    *,
    semana_inicio,
    departamento=None,
    metrica=None,
):
    """Aplica el impacto porcentual registrado a una predicción semanal."""
    resumen = impacto_factores_en_periodo(
        desde=semana_inicio,
        hasta=semana_inicio + timedelta(days=6),
        departamento=departamento,
        metrica=metrica,
    )
    impacto = resumen['impacto_pct']
    ajustado = Decimal(str(valor or 0)) * (Decimal('1') + impacto / Decimal('100'))
    return ajustado, resumen


def senales_operativas_inventario(*, desde, hasta, skus):
    """Obtiene precios y retrasos reales de compras para enriquecer inventario."""
    skus = {sku for sku in skus if sku}
    historicos = {}
    detalles = OrdenCompraDetalle.objects.filter(
        orden__fecha_orden__range=(desde, hasta),
        sku__in=skus,
        precio_unitario__gt=0,
    ).values('sku', 'precio_unitario')
    for detalle in detalles:
        historicos.setdefault(detalle['sku'], []).append(Decimal(str(detalle['precio_unitario'])))

    actuales = {}
    precios_actuales = ProveedorMaterialPrecio.objects.filter(
        material__sku__in=skus,
        material__activo=True,
        proveedor__activo=True,
        precio_unitario__gt=0,
    ).values('material__sku', 'precio_unitario').order_by('material__sku', 'precio_unitario')
    for precio in precios_actuales:
        actuales.setdefault(precio['material__sku'], Decimal(str(precio['precio_unitario'])))

    retrasos = {}
    estados_pendientes = [
        OrdenCompra.EstadoOrden.APROBADA,
        OrdenCompra.EstadoOrden.ENVIADA,
        OrdenCompra.EstadoOrden.PARCIAL,
    ]
    ordenes = OrdenCompra.objects.filter(
        estado__in=estados_pendientes,
        fecha_prometida__isnull=False,
        fecha_prometida__lt=hasta,
    ).prefetch_related('detalles')
    for orden in ordenes:
        dias = max(0, (hasta - orden.fecha_prometida).days)
        for detalle in orden.detalles.all():
            if detalle.sku not in skus:
                continue
            retrasos.setdefault(detalle.sku, []).append(dias)

    resultado = {}
    for sku in skus:
        precios = historicos.get(sku, [])
        precio_actual = actuales.get(sku)
        precio_historico = sum(precios, Decimal('0')) / Decimal(len(precios)) if precios else precio_actual
        variacion = Decimal('0')
        if precio_actual is not None and precio_historico:
            variacion = (precio_actual - precio_historico) / precio_historico * Decimal('100')
        dias_retraso = retrasos.get(sku, [])
        resultado[sku] = {
            'precio_historico': precio_historico,
            'precio_actual': precio_actual,
            'variacion_precio_pct': variacion,
            'ordenes_retrasadas': len(dias_retraso),
            'dias_retraso_promedio': (
                sum(dias_retraso, 0) / len(dias_retraso) if dias_retraso else Decimal('0')
            ),
        }
    return resultado
