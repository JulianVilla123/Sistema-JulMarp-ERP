"""Contrato común para los pronósticos de todos los departamentos.

La interfaz puede crecer por departamento, pero los periodos, horizontes y
estados deben conservar la misma semántica para que los dashboards puedan
compararse sin conversiones ocultas.
"""

HORIZONTES_SEMANAS = (1, 4, 8, 12)
PERIODICIDAD_SEMANAL = 'SEMANAL'
ESTADOS_PREDICCION = ('verde', 'amarillo', 'rojo')
DEPARTAMENTOS_PREDICTIVOS = (
    'Inventario',
    'MFG',
    'Finanzas',
    'QA',
    'Mantenimiento',
)


def normalizar_horizonte_semanas(value, default=4):
    """Devuelve un horizonte válido en semanas para cualquier módulo."""
    try:
        horizonte = int(value)
    except (TypeError, ValueError):
        horizonte = default
    return horizonte if horizonte in HORIZONTES_SEMANAS else default


def horizonte_a_dias(horizonte_semanas):
    """Convierte semanas del contrato común a días de cálculo."""
    return normalizar_horizonte_semanas(horizonte_semanas) * 7


def metadatos_contrato(*, muestras=0, calidad='baja', fuente='', variables=None):
    """Metadatos mínimos que acompañan cada predicción registrada."""
    return {
        'contrato_version': '1.0',
        'periodicidad': PERIODICIDAD_SEMANAL,
        'unidad_horizonte': 'SEMANAS',
        'muestras': int(muestras or 0),
        'calidad_dataset': calidad or 'baja',
        'fuente_datos': fuente,
        'variables_modelo': list(variables or []),
    }
