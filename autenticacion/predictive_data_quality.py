"""Auditoría común de datos históricos para los modelos predictivos.

La auditoría es de solo lectura. Su objetivo es detectar problemas antes de
entrenar o publicar un pronóstico: semanas faltantes, pocas muestras y
valores que no representan una operación válida.
"""

from datetime import timedelta

from django.utils import timezone
from django.db.models import Q

from .models import (
    CuentaPorPagarCobrar,
    RegistroParoMantenimiento,
    RegistroScrapDefecto,
    ReporteFinanciero,
    ReporteKPIProduccion,
    SalidaLineaDetalle,
)


MINIMO_MUESTRAS_SEMANALES = 4


def semana_inicio(fecha):
    """Normaliza una fecha al lunes de su semana operativa."""
    return fecha - timedelta(days=fecha.weekday())


def semanas_esperadas(desde, hasta):
    """Devuelve semanas operativas completas de lunes a viernes."""
    cursor = semana_inicio(desde)
    if cursor < desde:
        cursor += timedelta(weeks=1)
    ultima = semana_inicio(hasta)
    resultado = []
    while cursor <= ultima and cursor + timedelta(days=4) <= hasta:
        resultado.append(cursor)
        cursor += timedelta(weeks=1)
    return resultado


def _semanas_en_intervalo(inicio, fin):
    """Devuelve semanas con cualquier traslape, útil para reportes operativos."""
    cursor = semana_inicio(inicio)
    ultima = semana_inicio(fin)
    resultado = []
    while cursor <= ultima:
        resultado.append(cursor)
        cursor += timedelta(weeks=1)
    return resultado


def _fechas_a_semanas(fechas):
    return {semana_inicio(fecha) for fecha in fechas if fecha is not None}


def _semanas_de_intervalo(inicio, fin, desde, hasta):
    inicio = max(inicio, desde)
    fin = min(fin, hasta)
    if inicio > fin:
        return set()
    return set(_semanas_en_intervalo(inicio, fin))


def _estado_dataset(registros, semanas_cubiertas, faltantes, invalidos, semanas_totales):
    if registros == 0:
        return 'CRITICO'
    if invalidos:
        return 'CRITICO'
    if semanas_cubiertas < min(MINIMO_MUESTRAS_SEMANALES, semanas_totales):
        return 'ADVERTENCIA'
    if faltantes:
        return 'ADVERTENCIA'
    return 'OK'


def _resultado_dataset(
    *,
    departamento,
    fuente,
    registros,
    semanas,
    esperadas,
    invalidos=0,
    detalles_invalidos=None,
):
    semanas = set(semanas)
    esperadas = list(esperadas)
    faltantes = [semana for semana in esperadas if semana not in semanas]
    estado = _estado_dataset(
        registros,
        len(semanas),
        faltantes,
        invalidos,
        len(esperadas),
    )
    mensajes = []
    if registros == 0:
        mensajes.append('No hay registros en el periodo auditado.')
    if len(semanas) < min(MINIMO_MUESTRAS_SEMANALES, len(esperadas)):
        mensajes.append(
            f'Se requieren al menos {MINIMO_MUESTRAS_SEMANALES} semanas '
            'para un pronóstico base estable.'
        )
    if faltantes:
        mensajes.append(f'Hay {len(faltantes)} semanas sin datos.')
    if invalidos:
        mensajes.append(f'Hay {invalidos} registros con valores inválidos.')
    if not mensajes:
        mensajes.append('Cobertura semanal y valores válidos.')

    return {
        'departamento': departamento,
        'fuente': fuente,
        'registros': registros,
        'semanas_cubiertas': len(semanas),
        'semanas_esperadas': len(esperadas),
        'semanas_faltantes': [semana.isoformat() for semana in faltantes],
        'registros_invalidos': invalidos,
        'detalles_invalidos': detalles_invalidos or [],
        'estado': estado,
        'mensajes': mensajes,
    }


def _auditar_inventario(desde, hasta, esperadas):
    detalles = SalidaLineaDetalle.objects.filter(
        salida__fecha_salida__range=(desde, hasta),
    )
    fechas = list(detalles.values_list('salida__fecha_salida', flat=True))
    invalidos_query = Q(cantidad_enviada__lte=0) | Q(sku='')
    invalidos = list(detalles.filter(invalidos_query).values_list('sku', 'cantidad_enviada')[:5])
    return _resultado_dataset(
        departamento='Inventario',
        fuente='Salidas de materiales a línea',
        registros=detalles.count(),
        semanas=_fechas_a_semanas(fechas),
        esperadas=esperadas,
        invalidos=detalles.filter(invalidos_query).count(),
        detalles_invalidos=[
            {'sku': sku, 'cantidad': str(cantidad)} for sku, cantidad in invalidos
        ],
    )


def _auditar_mfg(desde, hasta, esperadas):
    reportes = ReporteKPIProduccion.objects.filter(
        fecha_inicio__lte=hasta,
        fecha_fin__gte=desde,
    )
    semanas = set()
    invalidos = 0
    metricas_porcentaje = (
        'disponibilidad',
        'rendimiento',
        'calidad',
        'oee',
        'tasa_rechazo',
        'cumplimiento_ordenes',
        'utilizacion_maquinas',
        'utilizacion_personal',
        'utilizacion_recursos',
    )
    for reporte in reportes:
        semanas.update(
            _semanas_de_intervalo(
                reporte.fecha_inicio,
                reporte.fecha_fin,
                desde,
                hasta,
            )
        )
        if any(
            getattr(reporte, campo) is None
            or getattr(reporte, campo) < 0
            or getattr(reporte, campo) > 100
            for campo in metricas_porcentaje
        ) or reporte.costo_planificado < 0 or reporte.costo_real < 0:
            invalidos += 1
    return _resultado_dataset(
        departamento='MFG',
        fuente='Reportes KPI de producción',
        registros=reportes.count(),
        semanas=semanas,
        esperadas=esperadas,
        invalidos=invalidos,
    )


def _auditar_finanzas(desde, hasta, esperadas):
    cuentas = CuentaPorPagarCobrar.objects.filter(fecha_emision__range=(desde, hasta))
    reportes = ReporteFinanciero.objects.filter(
        fecha_inicio__lte=hasta,
        fecha_fin__gte=desde,
    )
    semanas = _fechas_a_semanas(cuentas.values_list('fecha_emision', flat=True))
    invalidos = cuentas.filter(monto_total__lt=0).count()
    for reporte in reportes:
        semanas.update(
            _semanas_de_intervalo(
                reporte.fecha_inicio,
                reporte.fecha_fin,
                desde,
                hasta,
            )
        )
        if not isinstance(reporte.indicadores, dict):
            invalidos += 1
    return _resultado_dataset(
        departamento='Finanzas',
        fuente='Cuentas por cobrar/pagar y reportes financieros',
        registros=cuentas.count() + reportes.count(),
        semanas=semanas,
        esperadas=esperadas,
        invalidos=invalidos,
    )


def _auditar_qa(desde, hasta, esperadas):
    # El defecto se crea durante la simulación o captura, pero su fecha
    # operativa es la de la orden/lote asociado. Usamos la fecha de creación
    # únicamente cuando no existe una referencia operativa.
    defectos = RegistroScrapDefecto.objects.filter(
        fecha_creacion__date__range=(desde, hasta),
    ).select_related('orden', 'lote')
    invalidos = defectos.filter(cantidad_defectos__lte=0).count()
    fechas = []
    for defecto in defectos:
        if defecto.orden_id and defecto.orden.fecha_fin_programada:
            fechas.append(defecto.orden.fecha_fin_programada)
        elif defecto.lote_id:
            fechas.append(defecto.lote.fecha_captura)
        else:
            fechas.append(defecto.fecha_creacion.date())
    return _resultado_dataset(
        departamento='QA',
        fuente='Registros de scrap y defectos',
        registros=defectos.count(),
        semanas=_fechas_a_semanas(fechas),
        esperadas=esperadas,
        invalidos=invalidos,
    )


def _auditar_mantenimiento(desde, hasta, esperadas):
    paros = RegistroParoMantenimiento.objects.filter(fecha_paro__range=(desde, hasta))
    invalidos = paros.filter(horas_paro__lte=0).count()
    return _resultado_dataset(
        departamento='Mantenimiento',
        fuente='Registro de paros de mantenimiento',
        registros=paros.count(),
        semanas=_fechas_a_semanas(paros.values_list('fecha_paro', flat=True)),
        esperadas=esperadas,
        invalidos=invalidos,
    )


def auditar_datos_predictivos(*, days=90, hasta=None):
    """Audita cobertura y consistencia por departamento sin modificar la BD."""
    if days <= 0:
        raise ValueError('days debe ser mayor que cero.')
    hasta = hasta or timezone.localdate()
    desde = hasta - timedelta(days=days - 1)
    esperadas = semanas_esperadas(desde, hasta)
    datasets = [
        _auditar_inventario(desde, hasta, esperadas),
        _auditar_mfg(desde, hasta, esperadas),
        _auditar_finanzas(desde, hasta, esperadas),
        _auditar_qa(desde, hasta, esperadas),
        _auditar_mantenimiento(desde, hasta, esperadas),
    ]
    return {
        'desde': desde.isoformat(),
        'hasta': hasta.isoformat(),
        'dias': days,
        'semanas_esperadas': len(esperadas),
        'datasets': datasets,
        'criticos': sum(dataset['estado'] == 'CRITICO' for dataset in datasets),
        'advertencias': sum(dataset['estado'] == 'ADVERTENCIA' for dataset in datasets),
        'correctos': sum(dataset['estado'] == 'OK' for dataset in datasets),
    }
