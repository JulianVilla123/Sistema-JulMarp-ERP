# Plan de análisis predictivo para el ERP

## Objetivo

Convertir el ERP en un sistema que no solo registre operaciones, sino que anticipe riesgos y recomiende acciones por departamento.

El flujo común será:

~~~text
Datos operativos → Dataset histórico → Modelo → Pronóstico/riesgo → Acción → Resultado real
~~~

## Estado actual

- Producción/MFG: pronóstico de KPI con tendencia lineal, rango de incertidumbre, comparación de métricas y alertas.
- Datos locales: SQLite habilitado para desarrollo; Supabase permanece desactivado.
- Simulación: comando disponible para generar 10 periodos semanales.
- Validación básica: manage.py check y comparación de predicción contra periodos anteriores.
- Pendientes principales: más datos reales, registro formal de predicciones, modelos alternativos, reentrenamiento y acciones integradas.

## Fases de trabajo

### Fase 1 — Base común y calidad de datos

1. Crear un catálogo común de métricas, fuentes, unidades, objetivos y umbrales.
2. Normalizar periodos, líneas, departamentos, turnos y responsables.
3. Registrar la calidad del dataset: muestras, nulos, duplicados y periodo cubierto.
4. Guardar cada predicción con fecha, modelo, versión, valor, rango y confianza.
5. Comparar automáticamente cada predicción con el resultado real.

Salida: cualquier módulo podrá consumir predicciones con el mismo formato.

### Fase 2 — MFG/Producción

1. Mantener el pronóstico de OEE, disponibilidad, rendimiento, calidad y producción.
2. Separar predicción por línea, producto y turno.
3. Agregar predicción de scrap, retraso de órdenes y costo final.
4. Comparar regresión lineal, promedio móvil y modelos de árboles/series temporales.
5. Convertir alertas en tareas: revisar paros, ciclo, calidad o capacidad.

Criterio: el sistema debe mostrar el error histórico del modelo y no predecir con menos de 4 periodos.

### Fase 3 — Inventario y Compras

Predicciones:

- Demanda futura por material.
- Punto de reorden.
- Riesgo de ruptura de stock.
- Fecha probable de agotamiento.
- Tiempo de entrega de proveedores.
- Cantidad sugerida de compra.

Datos base: entradas, salidas, transferencias, inventario, proveedores, precios y órdenes de compra.

### Fase 4 — Calidad y QA

Predicciones:

- Probabilidad de defecto por producto, BOM, línea o proveedor.
- Tendencia de scrap.
- Reincidencia de defectos.
- Riesgo de reclamo de cliente.
- Proceso o material con mayor contribución al problema.

La pantalla debe mostrar causa probable, severidad, tendencia y acción correctiva sugerida.

### Fase 5 — Mantenimiento y recursos

Actualmente no existe un módulo formal de mantenimiento. Se debe comenzar con:

- Historial de paros.
- Horas de máquina.
- Costos por recurso.
- Cambios de ciclo.
- Defectos relacionados con máquina.

Después se agregará una bitácora de mantenimiento y, si existen sensores, variables como temperatura, vibración y consumo.

### Fase 6 — Finanzas y Planeación

Predicciones:

- Flujo de caja.
- Cobros y pagos atrasados.
- Desviación presupuestal.
- Costo final de órdenes.
- Rentabilidad estimada.
- Capacidad futura contra demanda.

Las predicciones financieras deben conservar trazabilidad, permisos por rol y explicación de los factores que influyen en el resultado.

### Fase 7 — Operación del sistema predictivo

1. Reentrenamiento programado.
2. Versionado de modelos.
3. Monitoreo de error y deriva de datos.
4. Registro de predicciones y decisiones.
5. Permisos por departamento.
6. Respaldos y migración a PostgreSQL para producción.
7. Pruebas automatizadas para datos, modelos y vistas.

## Prioridad inmediata

El siguiente bloque de implementación será la Fase 1:

1. Crear un contrato común para las predicciones.
2. Registrar predicciones y resultados reales.
3. Exponer una utilidad reutilizable para confianza, estado, tendencia y acción.
4. Migrar MFG a ese contrato.
5. Usar el mismo contrato para Inventario y Compras.

## Criterios de éxito

- Cada predicción identifica fuente, periodo, modelo y versión.
- El usuario ve valor actual, valor esperado, confianza, rango y acción.
- No se muestra alta confianza con pocos datos.
- El sistema puede medir el error después del periodo pronosticado.
- Cada departamento tiene al menos un caso predictivo útil.
- Las alertas pueden convertirse en tareas o seguimientos dentro del ERP.
