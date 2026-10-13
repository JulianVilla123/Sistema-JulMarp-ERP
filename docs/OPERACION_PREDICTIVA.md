# Operación del sistema predictivo

## Ciclo operativo

El ciclo centraliza el cálculo, registro, evaluación y monitoreo de las predicciones de MFG, Inventario, QA, Mantenimiento y Finanzas.

```powershell
venv\Scripts\python.exe manage.py ejecutar_ciclo_predictivo --usuario admin_ml
```

Para integrarlo con un programador de tareas y facilitar la integración posterior, se puede solicitar salida JSON:

```powershell
venv\Scripts\python.exe manage.py ejecutar_ciclo_predictivo --usuario admin_ml --json
```

El comando devuelve:

- versión del modelo utilizada;
- predicciones registradas por departamento;
- periodos evaluados contra resultados reales;
- pendientes vencidas;
- error y deriva detectada por departamento;
- versiones activas de cada modelo.

## Versionado

La versión activa se concentra en `VERSION_MODELO_ACTUAL` dentro de `autenticacion/ml_analytics.py`. La versión actual es `1.1`; las predicciones anteriores conservan `1.0` para mantener trazabilidad y comparación histórica.

## Monitoreo

`monitorear_salud_predictiva()` clasifica cada departamento:

- Verde: error controlado y seguimiento normal.
- Amarillo: faltan resultados reales o historial suficiente.
- Rojo: el error porcentual medio sugiere deriva y requiere recalibración.

## Operación segura

- La ejecución utiliza la base configurada por el proyecto; actualmente `USE_SUPABASE_DB=false` y la base activa es SQLite local.
- El comando no elimina predicciones históricas.
- Los registros se actualizan de forma idempotente por periodo, métrica, modelo y versión.
- Los módulos web mantienen permisos por departamento; el usuario de operación debe ser un usuario ERP activo autorizado.
