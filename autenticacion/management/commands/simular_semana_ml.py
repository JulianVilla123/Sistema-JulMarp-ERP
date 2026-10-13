from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from autenticacion.kpi_produccion import generar_reporte_kpis_produccion
from autenticacion.models import (
    Almacen,
    BOM,
    BOMDetalle,
    BOMOperacion,
    CostoHoraMaquina,
    CostoHoraOperador,
    Departamento,
    InformeValidacionDefectoQA,
    InventarioAlmacen,
    LoteProduccion,
    Material,
    OrdenFabricacion,
    OrdenFabricacionDetalle,
    PlanProduccion,
    Proveedor,
    ProveedorMaterialPrecio,
    RecepcionMaterial,
    RecepcionMaterialDetalle,
    RegistroScrapDefecto,
    RegistroUsoRecursoProduccion,
    ReporteKPIProduccion,
    SalidaLinea,
    SalidaLineaDetalle,
)


class Command(BaseCommand):
    help = 'Simula periodos de produccion con multiples segmentos para alimentar el dataset de ML.'

    marker = 'ML_SEMANA_PRODUCCION_SEGMENTADA_2026'
    legacy_marker = 'ML_SEMANA_PRODUCCION_850_2026'

    def add_arguments(self, parser):
        parser.add_argument(
            '--periods',
            type=int,
            default=10,
            help='Cantidad de periodos semanales a simular (por defecto: 10).',
        )

    def handle(self, *args, **options):
        periods = options['periods']
        if periods < 1:
            raise ValueError('--periods debe ser mayor que cero.')

        today = timezone.localdate()
        current_week_start = today - timedelta(days=today.weekday()) - timedelta(days=7)

        with transaction.atomic():
            users = self.ensure_users()
            catalog = self.ensure_catalog(users['admin'])
            self.clear_previous_simulation()
            results = []
            reports = []
            for period_index in range(1, periods + 1):
                week_start = current_week_start - timedelta(days=(periods - period_index) * 7)
                week_end = week_start + timedelta(days=4)
                for scenario in catalog['scenarios']:
                    results.append(
                        self.seed_week(
                            users,
                            scenario,
                            week_start,
                            week_end,
                            period_index,
                        )
                    )
                reports.append(
                    generar_reporte_kpis_produccion(
                        usuario=users['produccion'],
                        fecha_inicio=week_start,
                        fecha_fin=week_end,
                    )
                )

        total_produced = sum((item['orden'].cantidad_producida for item in results), Decimal('0'))
        total_scrap = sum((item['scrap_total'] for item in results), Decimal('0'))
        total_input = sum((item['entrada_material'] for item in results), Decimal('0'))
        total_consumption = sum((item['consumo_material'] for item in results), Decimal('0'))
        self.stdout.write(self.style.SUCCESS(f'{periods} periodos ML segmentados simulados correctamente.'))
        self.stdout.write(f"Segmentos: {len(catalog['scenarios'])} combinaciones de linea, producto y turno")
        self.stdout.write(f"Ordenes generadas: {len(results)}")
        self.stdout.write(f"Periodo: {results[0]['week_start']} a {results[-1]['week_end']}")
        self.stdout.write(f"Produccion total: {total_produced} piezas")
        self.stdout.write(f"Scrap total: {total_scrap} piezas")
        self.stdout.write(f"Entrada materiales total: {total_input} unidades")
        self.stdout.write(f"Consumo real total: {total_consumption} unidades")
        self.stdout.write(f"Diferencia material total: {total_input - total_consumption} unidades")
        self.stdout.write(f"Costo real recursos estimado: {Decimal('16000') * periods:.2f} MXN")
        self.stdout.write(f"Reportes KPI generados: {reports[0].id} a {reports[-1].id}")

    def ensure_users(self):
        User = get_user_model()

        departments = {}
        for name in ['Admin', 'Produccion', 'Inventario', 'QA', 'RRHH']:
            departments[name], _ = Departamento.objects.get_or_create(
                nombre=name,
                defaults={'descripcion': f'Departamento {name}', 'activo': True},
            )

        def user(username, department, staff=False, superuser=False):
            obj, _ = User.objects.get_or_create(
                username=username,
                defaults={
                    'email': f'{username}@julmarp.local',
                    'first_name': username.title(),
                    'last_name': 'ML',
                    'numero_empleado': f'ML-{username.upper()}',
                    'departamento': departments[department],
                    'is_staff': staff,
                    'is_superuser': superuser,
                    'activo': True,
                },
            )
            obj.departamento = departments[department]
            obj.is_staff = obj.is_staff or staff
            obj.is_superuser = obj.is_superuser or superuser
            obj.activo = True
            obj.is_active = True
            if not obj.has_usable_password():
                obj.set_unusable_password()
            obj.save()
            return obj

        return {
            'admin': user('admin_ml', 'Admin', True, True),
            'produccion': user('produccion_ml', 'Produccion'),
            'inventario': user('inventario_ml', 'Inventario'),
            'qa': user('qa_ml', 'QA'),
            'operador': user('operador_ml', 'RRHH'),
        }

    def scenario_specs(self):
        """Scenarios intentionally differ so the segmented forecast is explainable."""
        return [
            {
                'code': 'A',
                'line': 'Linea ML-01',
                'product': 'Producto Alfa ML',
                'shift': 'Matutino',
                'base': Decimal('620'),
                'trend': Decimal('35'),
                'variation': [0, 12, -8, 15, -5, 10, 6, -10, 18, 8],
                'material_factor': Decimal('1.06'),
                'scrap_rate': Decimal('0.025'),
                'machine_cost': Decimal('250'),
                'operator_cost': Decimal('150'),
                'cycle_minutes': Decimal('2.60'),
            },
            {
                'code': 'B',
                'line': 'Linea ML-02',
                'product': 'Producto Beta ML',
                'shift': 'Vespertino',
                'base': Decimal('920'),
                'trend': Decimal('-28'),
                'variation': [0, -10, 8, -12, 5, -8, 10, -6, 4, -10],
                'material_factor': Decimal('1.08'),
                'scrap_rate': Decimal('0.045'),
                'machine_cost': Decimal('275'),
                'operator_cost': Decimal('165'),
                'cycle_minutes': Decimal('3.10'),
            },
            {
                'code': 'C',
                'line': 'Linea ML-03',
                'product': 'Producto Gamma ML',
                'shift': 'Nocturno',
                'base': Decimal('760'),
                'trend': Decimal('0'),
                'variation': [0, 18, -12, 10, -6, 14, -10, 8, -4, 12],
                'material_factor': Decimal('1.04'),
                'scrap_rate': Decimal('0.032'),
                'machine_cost': Decimal('230'),
                'operator_cost': Decimal('140'),
                'cycle_minutes': Decimal('2.85'),
            },
        ]

    def ensure_catalog(self, admin):
        scenarios = []
        for spec in self.scenario_specs():
            almacen, _ = Almacen.objects.get_or_create(
                codigo=f"ML-ALM-{spec['code']}",
                defaults={
                    'nombre': f"Almacen {spec['product']}",
                    'descripcion': self.marker,
                    'activo': True,
                },
            )
            material, _ = Material.objects.get_or_create(
                sku=f"ML-MP-{spec['code']}",
                defaults={
                    'nombre': f"Materia prima {spec['product']}",
                    'descripcion': self.marker,
                    'um': 'PZA',
                    'activo': True,
                },
            )
            proveedor, _ = Proveedor.objects.get_or_create(
                nombre=f"Proveedor {spec['product']}",
                defaults={
                    'descripcion': self.marker,
                    'email': f"proveedor-{spec['code'].lower()}@julmarp.local",
                    'activo': True,
                },
            )
            proveedor.materiales.add(material)
            ProveedorMaterialPrecio.objects.update_or_create(
                proveedor=proveedor,
                material=material,
                defaults={'precio_unitario': Decimal('0.00')},
            )
            bom, _ = BOM.objects.get_or_create(
                codigo=f"BOM-ML-{spec['code']}",
                version='1.0',
                defaults={'creado_por': admin},
            )
            bom.tipo = BOM.TipoBOM.MFG
            bom.producto = spec['product']
            bom.descripcion = self.marker
            bom.cantidad_base = Decimal('1.00')
            bom.unidad_producto = 'PZA'
            bom.activo = True
            bom.creado_por = admin
            bom.save()
            BOMDetalle.objects.update_or_create(
                bom=bom,
                material=material,
                defaults={'cantidad': spec['material_factor'], 'observaciones': self.marker},
            )
            BOMOperacion.objects.update_or_create(
                bom=bom,
                secuencia=1,
                defaults={
                    'nombre': f"Produccion {spec['product']}",
                    'descripcion': self.marker,
                    'linea_produccion': spec['line'],
                    'tiempo_estimado': spec['cycle_minutes'],
                    'unidad_tiempo': BOMOperacion.UnidadTiempo.MINUTOS,
                    'recurso_maquina': f"Maquina {spec['code']}",
                    'operadores_requeridos': 1,
                },
            )
            spec = {**spec, 'almacen': almacen, 'material': material, 'proveedor': proveedor, 'bom': bom}
            scenarios.append(spec)
        return {'scenarios': scenarios}

    def clear_previous_simulation(self):
        ReporteKPIProduccion.objects.filter(generado_por__username='produccion_ml').delete()
        for marker in (self.marker, self.legacy_marker):
            RegistroScrapDefecto.objects.filter(descripcion__icontains=marker).delete()
            RegistroUsoRecursoProduccion.objects.filter(notas__icontains=marker).delete()
            LoteProduccion.objects.filter(observaciones__icontains=marker).delete()
            OrdenFabricacion.objects.filter(observaciones__icontains=marker).delete()
            PlanProduccion.objects.filter(observaciones__icontains=marker).delete()
            SalidaLinea.objects.filter(observaciones__icontains=marker).delete()
            RecepcionMaterial.objects.filter(observaciones__icontains=marker).delete()
            InventarioAlmacen.objects.filter(material__descripcion__icontains=marker).delete()

    def seed_week(self, users, scenario, week_start, week_end, period_index):
        period_tag = f"P{period_index:02d}"
        code = scenario['code']
        produced = (
            scenario['base']
            + scenario['trend'] * Decimal(period_index - 1)
            + Decimal(str(scenario['variation'][(period_index - 1) % len(scenario['variation'])]))
        ).quantize(Decimal('0.01'))
        planned = (produced + Decimal('18') + Decimal(str((period_index + ord(code)) % 5))).quantize(Decimal('0.01'))
        required_material = (produced * scenario['material_factor']).quantize(Decimal('0.001'))
        scrap_total = (produced * scenario['scrap_rate']).quantize(Decimal('0.01'))
        machine_hours = (produced / Decimal('20')).quantize(Decimal('0.01'))
        lot_code = f"LOTE-ML-{code}-{period_tag}"
        plan_code = f"PLAN-ML-{code}-{period_tag}"
        order_code = f"OF-ML-{code}-{period_tag}"
        start_dt = timezone.make_aware(datetime.combine(week_start, time(8, 0)))
        end_dt = timezone.make_aware(datetime.combine(week_end, time(16, 0)))

        recepcion = RecepcionMaterial.objects.create(
            fecha_recepcion=week_start,
            hora_recepcion=time(8, 0),
            proveedor=scenario['proveedor'].nombre,
            proveedor_registrado=scenario['proveedor'],
            orden_compra=f"OC-ML-{code}-{period_tag}",
            factura=f"FAC-ML-{code}-{period_tag}",
            transportista='Simulacion interna segmentada',
            placas='ML-2026',
            chk_oc=True,
            chk_cantidad=True,
            chk_empaque=True,
            chk_lote=True,
            chk_vigencia=True,
            chk_certificado=True,
            chk_estado_fisico=True,
            chk_calidad=True,
            observaciones=f"{self.marker} | segmento={code} entrada={required_material}",
            accion_recomendada=RecepcionMaterial.AccionRecomendada.ACEPTAR_TODO,
            estado=RecepcionMaterial.EstadoRecepcion.ENVIADA,
            creado_por=users['inventario'],
        )
        RecepcionMaterialDetalle.objects.create(
            recepcion=recepcion,
            material=scenario['material'],
            sku=scenario['material'].sku,
            descripcion=scenario['material'].nombre,
            um=scenario['material'].um,
            cantidad_oc=required_material,
            cantidad_recibida=required_material,
            lote=lot_code,
            ubicacion_destino=scenario['almacen'].codigo,
            estatus=RecepcionMaterialDetalle.EstatusDetalle.ACEPTADO,
        )
        inventario, _ = InventarioAlmacen.objects.get_or_create(
            material=scenario['material'],
            almacen=scenario['almacen'],
            lote=lot_code,
            defaults={'stock_actual': Decimal('0.00')},
        )
        inventario.stock_actual = (required_material - produced).quantize(Decimal('0.01'))
        inventario.save(update_fields=['stock_actual', 'fecha_actualizacion'])

        plan = PlanProduccion.objects.create(
            folio=plan_code,
            bom=scenario['bom'],
            cantidad_planificada=planned,
            fecha_inicio=week_start,
            fecha_fin=week_end,
            linea_produccion=scenario['line'],
            turno=scenario['shift'],
            observaciones=f"{self.marker} | segmento={code} plan={planned}",
            estado=PlanProduccion.EstadoPlan.COMPLETADO,
            creado_por=users['produccion'],
        )
        orden = OrdenFabricacion.objects.create(
            folio=order_code,
            plan=plan,
            bom=scenario['bom'],
            cantidad_planificada=planned,
            cantidad_producida=produced,
            linea_produccion=scenario['line'],
            turno=scenario['shift'],
            estado=OrdenFabricacion.EstadoOF.COMPLETADA,
            fecha_inicio_programada=week_start,
            fecha_fin_programada=week_end,
            fecha_inicio_real=start_dt,
            fecha_fin_real=end_dt,
            observaciones=(
                f"{self.marker} | segmento={code} produccion={produced} "
                f"entrada={required_material} scrap={scrap_total}"
            ),
            creado_por=users['produccion'],
        )
        OrdenFabricacionDetalle.objects.create(
            orden=orden,
            material=scenario['material'],
            cantidad_requerida=required_material,
            cantidad_consumida=produced,
            observaciones=f"{self.marker} | segmento={code}",
        )
        lote = LoteProduccion.objects.create(
            folio=lot_code,
            bom=scenario['bom'],
            orden_fabricacion=orden,
            fecha_captura=week_end,
            hora_captura=time(16, 0),
            linea_produccion=scenario['line'],
            turno=scenario['shift'],
            cantidad_producida=produced,
            operador='operador_ml',
            estado=LoteProduccion.EstadoLote.VALIDADO,
            observaciones=f"{self.marker} | segmento={code}",
            creado_por=users['produccion'],
        )

        salida = SalidaLinea.objects.create(
            fecha_salida=week_start,
            hora_salida=time(9, 0),
            linea_destino=scenario['line'],
            orden_produccion=orden.folio,
            turno=scenario['shift'],
            observaciones=f"{self.marker} | segmento={code} consumo={produced}",
            creado_por=users['inventario'],
        )
        SalidaLineaDetalle.objects.create(
            salida=salida,
            almacen_origen=scenario['almacen'],
            material=scenario['material'],
            sku=scenario['material'].sku,
            descripcion=scenario['material'].nombre,
            um=scenario['material'].um,
            cantidad_enviada=produced,
            lote=lot_code,
        )

        machine_cost, _ = CostoHoraMaquina.objects.update_or_create(
            linea_produccion=scenario['line'],
            maquina_nombre=f"Maquina {code}",
            defaults={
                'costo_hora': scenario['machine_cost'],
                'activo': True,
                'notas': self.marker,
                'registrado_por': users['produccion'],
                'actualizado_por': users['produccion'],
            },
        )
        operator_cost, _ = CostoHoraOperador.objects.update_or_create(
            operador=users['operador'],
            defaults={
                'nomina_hora': scenario['operator_cost'],
                'porcentaje_asistencia': Decimal('100.00'),
                'factor_desempeno': Decimal('100.00'),
                'activo': True,
                'notas': self.marker,
                'registrado_por': users['produccion'],
                'actualizado_por': users['produccion'],
            },
        )
        RegistroUsoRecursoProduccion.objects.create(
            orden=orden,
            tipo_recurso=RegistroUsoRecursoProduccion.TipoRecurso.MAQUINA,
            costo_maquina=machine_cost,
            horas_reales=machine_hours,
            notas=f"{self.marker} | segmento={code} horas={machine_hours}",
            registrado_por=users['produccion'],
            actualizado_por=users['produccion'],
        )
        RegistroUsoRecursoProduccion.objects.create(
            orden=orden,
            tipo_recurso=RegistroUsoRecursoProduccion.TipoRecurso.OPERADOR,
            costo_operador=operator_cost,
            horas_reales=machine_hours,
            notas=f"{self.marker} | segmento={code} horas={machine_hours}",
            registrado_por=users['produccion'],
            actualizado_por=users['produccion'],
        )

        scrap_specs = [
            (RegistroScrapDefecto.TipoDefecto.DIMENSIONAL, 'Dimensional', Decimal('0.45')),
            (RegistroScrapDefecto.TipoDefecto.VISUAL, 'Superficie', Decimal('0.30')),
            (RegistroScrapDefecto.TipoDefecto.PROCESO, 'Ensamble', Decimal('0.25')),
        ]
        for defect_type, cause, ratio in scrap_specs:
            qty = (scrap_total * ratio).quantize(Decimal('0.01'))
            scrap = RegistroScrapDefecto.objects.create(
                orden=orden,
                lote=lote,
                cantidad_defectos=qty,
                tipo_defecto=defect_type,
                causa=cause,
                descripcion=f"{self.marker} | segmento={code} defecto={cause}",
                registrado_por=users['produccion'],
                actualizado_por=users['produccion'],
            )
            InformeValidacionDefectoQA.objects.create(
                defecto=scrap,
                resultado_validacion=InformeValidacionDefectoQA.ResultadoValidacion.VALIDADO,
                falla_maquina=cause == 'Dimensional',
                informe=f"{self.marker} | segmento={code} validacion QA {cause}",
                acciones_contencion='Ajuste de parametros y revision de proceso.',
                validado_por=users['qa'],
            )

        return {
            'orden': orden,
            'week_start': week_start,
            'week_end': week_end,
            'scrap_total': scrap_total,
            'entrada_material': required_material,
            'consumo_material': produced,
        }
