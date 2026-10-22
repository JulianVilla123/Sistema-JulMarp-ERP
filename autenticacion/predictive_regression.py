"""Motor pequeño y reutilizable para regresión lineal múltiple.

La primera versión del ERP calcula tendencias con una sola variable: el tiempo.
Este módulo prepara la siguiente etapa sin depender de numpy o scikit-learn,
que todavía no forman parte del entorno local. La integración con MFG y
Finanzas se hará después de validar este motor de forma aislada.
"""

from decimal import Decimal, InvalidOperation
from typing import Sequence


ZERO = Decimal('0')
ONE = Decimal('1')


def _decimal(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f'Valor numérico inválido: {value!r}')


def _solve_linear_system(matrix: list[list[Decimal]], vector: list[Decimal]) -> list[Decimal]:
    """Resuelve A*x=b mediante eliminación de Gauss con pivoteo parcial."""
    size = len(vector)
    augmented = [row[:] + [vector[index]] for index, row in enumerate(matrix)]

    for column in range(size):
        pivot_row = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        pivot = augmented[pivot_row][column]
        if pivot == ZERO:
            raise ValueError('Las variables predictoras son linealmente dependientes.')
        if pivot_row != column:
            augmented[column], augmented[pivot_row] = augmented[pivot_row], augmented[column]

        pivot = augmented[column][column]
        augmented[column] = [value / pivot for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            if factor == ZERO:
                continue
            augmented[row] = [
                current - factor * normalized
                for current, normalized in zip(augmented[row], augmented[column])
            ]

    return [augmented[row][-1] for row in range(size)]


def _prepare_rows(features: Sequence[Sequence], targets: Sequence):
    rows = []
    for row, target in zip(features, targets):
        try:
            clean_row = [_decimal(value) for value in row]
            clean_target = _decimal(target)
        except ValueError:
            continue
        if clean_row:
            rows.append((clean_row, clean_target))
    return rows


def validar_datos_regresion_multiple(
    features: Sequence[Sequence],
    targets: Sequence,
    feature_names: Sequence[str],
    *,
    min_samples: int | None = None,
) -> dict:
    """Valida la matriz predictora antes de entrenar o evaluar."""
    names = [str(name).strip() for name in feature_names]
    feature_count = len(names)
    required = min_samples if min_samples is not None else max(4, feature_count + 2)
    issues = []
    if not names or any(not name for name in names):
        issues.append('Se necesita al menos una variable predictora con nombre.')
    if len(features) != len(targets):
        issues.append('Las variables y objetivos no tienen la misma cantidad de filas.')
    if len(features) < required:
        issues.append(f'Se necesitan al menos {required} observaciones para {feature_count} variables.')
    if len(names) != len(set(names)):
        issues.append('Los nombres de las variables predictoras deben ser únicos.')

    for index, row in enumerate(features):
        if len(row) != feature_count:
            issues.append(f'La fila {index + 1} no tiene {feature_count} variables.')
            continue
        for value in row:
            try:
                if not _decimal(value).is_finite():
                    issues.append(f'La fila {index + 1} contiene un valor no finito.')
                    break
            except ValueError:
                issues.append(f'La fila {index + 1} contiene un valor no numérico.')
                break
    for index, target in enumerate(targets):
        try:
            if not _decimal(target).is_finite():
                issues.append(f'El objetivo de la fila {index + 1} no es finito.')
        except ValueError:
            issues.append(f'El objetivo de la fila {index + 1} no es numérico.')

    return {
        'valid': not issues,
        'samples': len(features),
        'features': names,
        'feature_count': feature_count,
        'required_samples': required,
        'issues': issues[:10],
    }


def predecir_regresion_multiple(model: dict, features: Sequence[Sequence]) -> list[Decimal]:
    """Genera predicciones con un modelo previamente ajustado."""
    if not model.get('enabled'):
        return []
    intercept = _decimal(model['intercepto'])
    coefficients = [_decimal(value) for value in model['coeficientes'].values()]
    predictions = []
    for row in features:
        values = [_decimal(value) for value in row]
        if len(values) != len(coefficients):
            raise ValueError('La cantidad de variables no coincide con el modelo.')
        predictions.append(intercept + sum((coef * value for coef, value in zip(coefficients, values)), ZERO))
    return predictions


def ajustar_regresion_multiple(
    features: Sequence[Sequence],
    targets: Sequence,
    feature_names: Sequence[str],
    *,
    min_samples: int | None = None,
    ridge_alpha: Decimal | str | int = '0.01',
) -> dict:
    """Ajusta una regresión múltiple y devuelve métricas auditables.

    ``features`` debe contener una fila por periodo y una columna por variable.
    La primera columna del sistema interno es el intercepto; no se regulariza.
    ``ridge_alpha`` evita que pequeñas redundancias numéricas hagan fallar el
    ajuste. No sustituye la validación temporal que se añadirá en la integración.
    """
    names = [str(name) for name in feature_names]
    feature_count = len(names)
    required = min_samples if min_samples is not None else max(4, feature_count + 2)
    empty_result = {
        'enabled': False,
        'method': 'Regresión lineal múltiple',
        'samples': len(features),
        'features': names,
        'feature_count': feature_count,
        'intercepto': ZERO,
        'coeficientes': {name: ZERO for name in names},
        'predictions': [],
        'mae': ZERO,
        'mape': None,
        'r2': None,
        'reason': '',
    }
    validation = validar_datos_regresion_multiple(
        features,
        targets,
        names,
        min_samples=required,
    )
    if not validation['valid']:
        empty_result['reason'] = ' '.join(validation['issues'])
        return empty_result
    rows = _prepare_rows(features, targets)

    alpha = _decimal(ridge_alpha)
    if not alpha.is_finite() or alpha < ZERO:
        empty_result['reason'] = 'La regularización debe ser un número finito no negativo.'
        return empty_result
    dimension = feature_count + 1
    design = [[ONE] + row for row, _ in rows]
    normal_matrix = [[ZERO for _ in range(dimension)] for _ in range(dimension)]
    normal_vector = [ZERO for _ in range(dimension)]
    for row, (_, target) in zip(design, rows):
        for left in range(dimension):
            normal_vector[left] += row[left] * target
            for right in range(dimension):
                normal_matrix[left][right] += row[left] * row[right]
    for index in range(1, dimension):
        normal_matrix[index][index] += alpha

    try:
        solution = _solve_linear_system(normal_matrix, normal_vector)
    except ValueError as error:
        empty_result['reason'] = str(error)
        return empty_result

    intercept = solution[0]
    coefficients = dict(zip(names, solution[1:]))
    predictions = [intercept + sum((coef * value for coef, value in zip(solution[1:], row)), ZERO) for row, _ in rows]
    targets_clean = [target for _, target in rows]
    residuals = [target - prediction for target, prediction in zip(targets_clean, predictions)]
    mae = sum((abs(value) for value in residuals), ZERO) / Decimal(len(residuals))
    non_zero_targets = [(target, prediction) for target, prediction in zip(targets_clean, predictions) if target != ZERO]
    mape = None
    if non_zero_targets:
        mape = sum((abs((target - prediction) / target) * Decimal('100') for target, prediction in non_zero_targets), ZERO) / Decimal(len(non_zero_targets))
    mean_target = sum(targets_clean, ZERO) / Decimal(len(targets_clean))
    total_variation = sum(((target - mean_target) ** 2 for target in targets_clean), ZERO)
    residual_variation = sum(((target - prediction) ** 2 for target, prediction in zip(targets_clean, predictions)), ZERO)
    r2 = None if total_variation == ZERO else ONE - (residual_variation / total_variation)

    return {
        **empty_result,
        'enabled': True,
        'intercepto': intercept,
        'coeficientes': coefficients,
        'predictions': predictions,
        'mae': mae,
        'mape': mape,
        'r2': r2,
        'reason': 'Modelo ajustado correctamente.',
    }


def evaluar_regresion_multiple_temporal(
    features: Sequence[Sequence],
    targets: Sequence,
    feature_names: Sequence[str],
    *,
    min_samples: int | None = None,
    ridge_alpha: Decimal | str | int = '0.01',
) -> dict:
    """Evalúa el modelo con validación hacia adelante, sin mezclar futuro."""
    names = [str(name) for name in feature_names]
    required = min_samples if min_samples is not None else max(4, len(names) + 2)
    validation = validar_datos_regresion_multiple(
        features,
        targets,
        names,
        min_samples=required,
    )
    if not validation['valid']:
        return {
            'backtests': 0,
            'mae': ZERO,
            'mape': None,
            'reason': ' '.join(validation['issues']),
        }

    actuals = []
    predictions = []
    for index in range(required, len(targets)):
        model = ajustar_regresion_multiple(
            features[:index],
            targets[:index],
            names,
            min_samples=required,
            ridge_alpha=ridge_alpha,
        )
        if not model['enabled']:
            continue
        prediction = predecir_regresion_multiple(model, [features[index]])[0]
        actuals.append(_decimal(targets[index]))
        predictions.append(prediction)

    if not predictions:
        return {
            'backtests': 0,
            'mae': ZERO,
            'mape': None,
            'reason': 'No hay suficientes periodos para validar temporalmente.',
        }
    errors = [abs(actual - prediction) for actual, prediction in zip(actuals, predictions)]
    non_zero = [(actual, prediction) for actual, prediction in zip(actuals, predictions) if actual != ZERO]
    mape = None
    if non_zero:
        mape = sum(
            (abs((actual - prediction) / actual) * Decimal('100') for actual, prediction in non_zero),
            ZERO,
        ) / Decimal(len(non_zero))
    return {
        'backtests': len(predictions),
        'mae': sum(errors, ZERO) / Decimal(len(errors)),
        'mape': mape,
        'reason': 'Validación temporal hacia adelante completada.',
    }
