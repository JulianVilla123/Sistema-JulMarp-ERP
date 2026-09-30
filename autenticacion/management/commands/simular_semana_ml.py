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
    SalidaLinea,
    SalidaLineaDetalle,
)


class Command(BaseCommand):
    help = 'Simula una semana de produccion y alimenta el dataset predictivo de ML.'

    marker = 'ML_SEMANA_PRODUCCION_850_2026'

    def handle(self, *args, **options):
        today = timezone.localdate()
        week_start = today - timedelta(days=today.weekday())
        week_end = week_start + timedelta(days=4)

        with transaction.atomic():
            users = self.ensure_users()
            catalog = self.ensure_catalog(users['admin'])
            self.clear_previous_simulation()
            result = self.seed_week(users, catalog, week_start, week_end)
            report = generar_reporte_kpis_produccion(
                usuario=users['produccion'],
                fecha_inicio=week_start,
                fecha_fin=week_end,
            )

        self.stdout.write(self.style.SUCCESS('Semana ML simulada correctamente.'))
        self.stdout.write(f"Periodo: {week_start} a {week_end}")
        self.stdout.write(f"Orden: {result['orden'].folio}")
        self.stdout.write(f"Produccion total: {result['orden'].cantidad_producida} piezas")
        self.stdout.write(f"Scrap: {result['scrap_total']} piezas ({report.tasa_rechazo}%)")
        self.stdout.write(f"Entrada materiales: {result['entrada_material']} unidades")
        self.stdout.write(f"Consumo real: {result['consumo_material']} unidades")
        self.stdout.write(f"Diferencia material: {result['entrada_material'] - result['consumo_material']} unidades")
        self.stdout.write(f"Costo real recursos: $16000.00 MXN")
        self.stdout.write(f"Reporte KPI generado: {report.id}")

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

    def ensure_catalog(self, admin):
        almacen, _ = Almacen.objects.get_or_create(
            codigo='ML-MP',
            defaults={'nombre': 'Almacen ML materia prima', 'descripcion': self.marker, 'activo': True},
        )
        material, _ = Material.objects.get_or_create(
            sku='ML-MP-001',
            defaults={
                'nombre': 'Materia prima simulacion ML',
                'descripcion': self.marker,
                'um': 'PZA',
                'activo': True,
            },
        )
        proveedor, _ = Proveedor.objects.get_or_create(
            nombre='Proveedor Simulacion ML',
            defaults={'descripcion': self.marker, 'email': 'ml-proveedor@julmarp.local', 'activo': True},
        )
        proveedor.materiales.add(material)
        ProveedorMaterialPrecio.objects.update_or_create(
            proveedor=proveedor,
            material=material,
            defaults={'precio_unitario': Decimal('0.00')},
        )
        bom, _ = BOM.objects.get_or_create(
            codigo='BOM-ML-850',
            version='1.0',
            defaults={
                'tipo': BOM.TipoBOM.MFG,
                'producto': 'Producto semana ML',
                'descripcion': self.marker,
                'cantidad_base': Decimal('1.00'),
                'unidad_producto': 'PZA',
                'activo': True,
                'creado_por': admin,
            },
        )
        bom.tipo = BOM.TipoBOM.MFG
        bom.activo = True
        bom.save(update_fields=['tipo', 'activo'])
        BOMDetalle.objects.update_or_create(
            bom=bom,
            material=material,
            defaults={'cantidad': Decimal('1.000'), 'observaciones': self.marker},
        )
        BOMOperacion.objects.update_or_create(
            bom=bom,
            secuencia=1,
            defaults={
                'nombre': 'Produccion semanal ML',
                'descripcion': self.marker,
                'linea_produccion': 'Linea ML-01',
                'tiempo_estimado': Decimal('2.82'),
                'unidad_tiempo': BOMOperacion.UnidadTiempo.MINUTOS,
                'recurso_maquina': 'Maquina ML-01',
                'operadores_requeridos': 1,
            },
        )
        return {'almacen': almacen, 'material': material, 'proveedor': proveedor, 'bom': bom}

    def clear_previous_simulation(self):
        RegistroScrapDefecto.objects.filter(descripcion__icontains=self.marker).delete()
        RegistroUsoRecursoProduccion.objects.filter(notas__icontains=self.marker).delete()
        LoteProduccion.objects.filter(observaciones__icontains=self.marker).delete()
        OrdenFabricacion.objects.filter(observaciones__icontains=self.marker).delete()
        PlanProduccion.objects.filter(observaciones__icontains=self.marker).delete()
        SalidaLinea.objects.filter(observaciones__icontains=self.marker).delete()
        RecepcionMaterial.objects.filter(observaciones__icontains=self.marker).delete()

    def seed_week(self, users, catalog, week_start, week_end):
        start_dt = timezone.make_aware(datetime.combine(week_start, time(8, 0)))
        end_dt = timezone.make_aware(datetime.combine(week_end, time(16, 0)))

        recepcion = RecepcionMaterial.objects.create(
            fecha_recepcion=week_start,
            hora_recepcion=time(8, 0),
            proveedor=catalog['proveedor'].nombre,
            proveedor_registrado=catalog['proveedor'],
            orden_compra='OC-ML-SEMANA',
            factura='FAC-ML-850',
            transportista='Simulacion interna',
            placas='ML-2026',
            chk_oc=True,
            chk_cantidad=True,
            chk_empaque=True,
            chk_lote=True,
            chk_vigencia=True,
            chk_certificado=True,
            chk_estado_fisico=True,
            chk_calidad=True,
            observaciones=f'{self.marker} | Entrada 900 unidades',
            accion_recomendada=RecepcionMaterial.AccionRecomendada.ACEPTAR_TODO,
            estado=RecepcionMaterial.EstadoRecepcion.ENVIADA,
            creado_por=users['inventario'],
        )
        RecepcionMaterialDetalle.objects.create(
            recepcion=recepcion,
            material=catalog['material'],
            sku=catalog['material'].sku,
            descripcion=catalog['material'].nombre,
            um=catalog['material'].um,
            cantidad_oc=Decimal('900.00'),
            cantidad_recibida=Decimal('900.00'),
            lote='LOTE-ML-SEMANA',
            ubicacion_destino=catalog['almacen'].codigo,
            estatus=RecepcionMaterialDetalle.EstatusDetalle.ACEPTADO,
        )
        inventario, _ = InventarioAlmacen.objects.get_or_create(
            material=catalog['material'],
            almacen=catalog['almacen'],
            lote='LOTE-ML-SEMANA',
            defaults={'stock_actual': Decimal('0.00')},
        )
        inventario.stock_actual = Decimal('50.00')
        inventario.save(update_fields=['stock_actual', 'fecha_actualizacion'])

        plan = PlanProduccion.objects.create(
            folio='PLAN-ML-SEMANA',
            bom=catalog['bom'],
            cantidad_planificada=Decimal('850.00'),
            fecha_inicio=week_start,
            fecha_fin=week_end,
            linea_produccion='Linea ML-01',
            turno='Semana',
            observaciones=f'{self.marker} | Produccion 850 piezas',
            estado=PlanProduccion.EstadoPlan.COMPLETADO,
            creado_por=users['produccion'],
        )
        orden = OrdenFabricacion.objects.create(
            folio='OF-ML-SEMANA',
            plan=plan,
            bom=catalog['bom'],
            cantidad_planificada=Decimal('850.00'),
            cantidad_producida=Decimal('850.00'),
            linea_produccion='Linea ML-01',
            turno='Semana',
            estado=OrdenFabricacion.EstadoOF.COMPLETADA,
            fecha_inicio_programada=week_start,
            fecha_fin_programada=week_end,
            fecha_inicio_real=start_dt,
            fecha_fin_real=end_dt,
            observaciones=f'{self.marker} | consumo=850 entrada=900 diferencia=50 costo_real=16000',
            creado_por=users['produccion'],
        )
        OrdenFabricacionDetalle.objects.create(
            orden=orden,
            material=catalog['material'],
            cantidad_requerida=Decimal('900.000'),
            cantidad_consumida=Decimal('850.000'),
            observaciones=f'{self.marker} | diferencia material 50',
        )
        lote = LoteProduccion.objects.create(
            folio='LOTE-ML-850',
            bom=catalog['bom'],
            orden_fabricacion=orden,
            fecha_captura=week_end,
            hora_captura=time(16, 0),
            linea_produccion='Linea ML-01',
            turno='Semana',
            cantidad_producida=Decimal('850.00'),
            operador='operador_ml',
            estado=LoteProduccion.EstadoLote.VALIDADO,
            observaciones=self.marker,
            creado_por=users['produccion'],
        )

        salida = SalidaLinea.objects.create(
            fecha_salida=week_start,
            hora_salida=time(9, 0),
            linea_destino='Linea ML-01',
            orden_produccion=orden.folio,
            turno='Semana',
            observaciones=f'{self.marker} | Consumo real 850 unidades',
            creado_por=users['inventario'],
        )
        SalidaLineaDetalle.objects.create(
            salida=salida,
            almacen_origen=catalog['almacen'],
            material=catalog['material'],
            sku=catalog['material'].sku,
            descripcion=catalog['material'].nombre,
            um=catalog['material'].um,
            cantidad_enviada=Decimal('850.00'),
            lote='LOTE-ML-SEMANA',
        )

        machine_cost, _ = CostoHoraMaquina.objects.update_or_create(
            linea_produccion='Linea ML-01',
            maquina_nombre='Maquina ML-01',
            defaults={
                'costo_hora': Decimal('250.00'),
                'activo': True,
                'notas': self.marker,
                'registrado_por': users['produccion'],
                'actualizado_por': users['produccion'],
            },
        )
        operator_cost, _ = CostoHoraOperador.objects.update_or_create(
            operador=users['operador'],
            defaults={
                'nomina_hora': Decimal('150.00'),
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
            horas_reales=Decimal('40.00'),
            notas=f'{self.marker} | 40h maquina x 250',
            registrado_por=users['produccion'],
            actualizado_por=users['produccion'],
        )
        RegistroUsoRecursoProduccion.objects.create(
            orden=orden,
            tipo_recurso=RegistroUsoRecursoProduccion.TipoRecurso.OPERADOR,
            costo_operador=operator_cost,
            horas_reales=Decimal('40.00'),
            notas=f'{self.marker} | 40h operador x 150',
            registrado_por=users['produccion'],
            actualizado_por=users['produccion'],
        )

        scrap_specs = [
            (RegistroScrapDefecto.TipoDefecto.DIMENSIONAL, 'Dimensional', Decimal('20.00')),
            (RegistroScrapDefecto.TipoDefecto.VISUAL, 'Superficie', Decimal('15.00')),
            (RegistroScrapDefecto.TipoDefecto.PROCESO, 'Ensamble', Decimal('10.00')),
        ]
        for defect_type, cause, qty in scrap_specs:
            scrap = RegistroScrapDefecto.objects.create(
                orden=orden,
                lote=lote,
                cantidad_defectos=qty,
                tipo_defecto=defect_type,
                causa=cause,
                descripcion=f'{self.marker} | defecto={cause}',
                registrado_por=users['produccion'],
                actualizado_por=users['produccion'],
            )
            InformeValidacionDefectoQA.objects.create(
                defecto=scrap,
                resultado_validacion=InformeValidacionDefectoQA.ResultadoValidacion.VALIDADO,
                falla_maquina=cause == 'Dimensional',
                informe=f'{self.marker} | Validacion QA de defecto {cause}',
                acciones_contencion='Ajuste de parametros y revision de proceso.',
                validado_por=users['qa'],
            )

        return {
            'orden': orden,
            'scrap_total': Decimal('45.00'),
            'entrada_material': Decimal('900.00'),
            'consumo_material': Decimal('850.00'),
        }
