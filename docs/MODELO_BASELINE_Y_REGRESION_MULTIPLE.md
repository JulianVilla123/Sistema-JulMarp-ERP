# Línea base y regresión lineal múltiple

## Línea base actual

MFG, Finanzas y Ventas utilizan actualmente una tendencia lineal simple por
periodo. El modelo toma una métrica histórica como variable dependiente y el
tiempo como variable predictora. Después puede aplicar factores externos
editables, como temporada, promoción, precio o retraso de proveedor.

Esta línea base se conserva para comparar el nuevo modelo y para servir como
respaldo cuando no existan suficientes observaciones.

## Nuevo modelo preparado

La regresión lineal múltiple utilizará varias variables predictoras por cada
periodo:

```text
y = intercepto + coeficiente_1 x variable_1 + ... + coeficiente_n x variable_n
```

La primera entrega incorpora el motor estadístico aislado. Todavía no cambia
los pronósticos visibles de MFG ni Finanzas; la conexión se realizará después
de validar las variables, los cierres semanales y la comparación contra la
línea base.

## Criterio de seguridad

El modelo requiere como mínimo cuatro observaciones y, por defecto, al menos
dos observaciones por cada variable predictora. Si no se cumple, devuelve un
estado no disponible y el sistema debe conservar la regresión simple.

## Validaciones incorporadas

Antes de ajustar el modelo se comprueba que:

- Las variables y los objetivos tengan la misma cantidad de filas.
- Todas las filas tengan la misma cantidad de variables.
- Los nombres de las variables sean únicos.
- Los valores sean numéricos y finitos.
- Exista el mínimo de observaciones requerido.
- La validación temporal utilice únicamente periodos anteriores para predecir
  un periodo posterior.
