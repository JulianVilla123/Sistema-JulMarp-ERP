from datetime import date, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from autenticacion.models import (
    Almacen,
    BitacoraAcceso,
    BOM,
    BOMDetalle,
    BOMOperacion,
    ClienteCompra,
    CosteoProduccion,
    CuentaContable,
    CuentaPorPagarCobrar,
    DeclaracionImpuesto,
    Departamento,
    HistorialCambioUsuario,
    InformeValidacionDefectoQA,
    InventarioAlmacen,
    LoteProduccion,
    Material,
    OrdenCompra,
    OrdenCompraDetalle,
    OrdenFabricacion,
    PlanProduccion,
    PlanProduccionDetalle,
    PolizaContable,
    PresupuestoFinanciero,
    Proveedor,
    RecepcionMaterial,
    RecepcionMaterialDetalle,
    ReclamoCliente,
    RegistroScrapDefecto,
    RegistroUsoRecursoProduccion,
    RequerimientoMaterialProduccion,
    RequerimientoMaterialProduccionDetalle,
    SalidaLinea,
    SalidaLineaDetalle,
    TicketSoporte,
    TransferenciaAlmacen,
    TransferenciaAlmacenDetalle,
)


class Command(BaseCommand):
    help = 'Crea datos demo semanales para todos los departamentos del ERP.'

    marker = 'DEMO_TESIS_SEMANA_2026W27'

    def handle(self, *args, **options):
        with transaction.atomic():
            users = self.ensure_users()
            catalogs = self.ensure_catalogs(users['admin'])

            if PlanProduccion.objects.filter(observaciones__icontains=self.marker).exists():
                self.stdout.write(self.style.WARNING('Los datos demo semanales ya existen. No se duplicó información.'))
                return

            inventory = self.seed_inventory(users, catalogs)
            production = self.seed_production(users, catalogs)
            finance = self.seed_finance(users, catalogs, production)
            quality = self.seed_quality(users, catalogs, production)
            it = self.seed_it(users, catalogs)

            self.stdout.write(self.style.SUCCESS(
                'Datos demo creados: '
                f"{inventory} movimientos inventario, {production['ofs']} OFs, "
                f"{finance} registros financieros, {quality} registros QA, {it} registros IT."
            ))

    def ensure_users(self):
        User = get_user_model()
        deptos = {d.nombre: d for d in Departamento.objects.all()}

        def make_user(username, departamento, staff=False, superuser=False):
            user, _ = User.objects.get_or_create(
                username=username,
                defaults={
                    'email': f'{username}@julmarp.local',
                    'first_name': departamento,
                    'last_name': 'Demo',
                    'departamento': deptos.get(departamento),
                    'numero_empleado': f'DEMO-{username.upper()}',
                    'is_staff': staff,
                    'is_superuser': superuser,
                    'activo': True,
                },
            )
            if not user.check_password(username):
                user.set_password(username)
            user.departamento = deptos.get(departamento)
            user.is_staff = staff
            user.is_superuser = superuser
            user.activo = True
            user.is_active = True
            user.save()
            return user

        return {
            'admin': make_user('admin2', 'Admin', True, True),
            'finanzas': make_user('finanzas2', 'Finanzas'),
            'it': make_user('it2', 'IT', True),
            'inventario': make_user('inventario2', 'Inventario'),
            'produccion': make_user('produccion2', 'Producción'),
            'qa': make_user('qa2', 'QA'),
            'rrhh': make_user('rrhh2', 'RRHH'),
        }

    def ensure_catalogs(self, admin):
        almacenes = {}
        for codigo, nombre in [
            ('MP', 'Materia prima'),
            ('QA', 'Cuarentena QA'),
            ('WIP', 'Producción WIP'),
            ('PT', 'Producto terminado'),
        ]:
            almacenes[codigo], _ = Almacen.objects.get_or_create(
                codigo=codigo,
                defaults={'nombre': nombre, 'descripcion': f'Almacén demo {nombre}', 'activo': True},
            )

        materiales = {}
        for sku, nombre, um in [
            ('MAT-0001', 'Lamina galvanizada 1mm', 'PZA'),
            ('MAT-0004', 'Resina ABS negra', 'SACO'),
            ('MAT-0008', 'Tornillo M4x20', 'PZA'),
            ('MAT-0014', 'Arnes electrico tipo A', 'PZA'),
            ('MAT-0021', 'Etiqueta codigo barras 50x30', 'ROLLO'),
            ('MAT-0036', 'Tarjeta PCB control', 'PZA'),
        ]:
            materiales[sku], _ = Material.objects.get_or_create(
                sku=sku,
                defaults={'nombre': nombre, 'um': um, 'activo': True},
            )

        proveedores = {}
        for nombre in [
            'Aceros Industriales del Norte',
            'Polímeros del Bajío',
            'Fasteners Plus',
            'Arneses Integrados SA',
            'PCB Solutions México',
        ]:
            proveedores[nombre], _ = Proveedor.objects.get_or_create(
                nombre=nombre,
                defaults={'email': f"ventas@{nombre.lower().replace(' ', '').replace('í', 'i')}.com", 'activo': True},
            )

        clientes = {}
        for codigo, nombre in [
            ('CLI-MAB', 'Mabe'),
            ('CLI-LG', 'LG Electronics Mexico'),
            ('CLI-SAM', 'Samsung Electronics Mexico'),
        ]:
            clientes[codigo], _ = ClienteCompra.objects.get_or_create(
                codigo=codigo,
                defaults={
                    'nombre': nombre,
                    'contacto': 'Compras corporativas',
                    'email': f'compras.{codigo.lower()}@cliente.local',
                    'activo': True,
                },
            )

        bom, _ = BOM.objects.get_or_create(
            codigo='BOM-DEMO-ERP-01',
            version='1.0',
            defaults={
                'tipo': BOM.TipoBOM.MFG,
                'producto': 'Modulo de control JulMarp',
                'descripcion': f'{self.marker} | Producto demo para tesis',
                'cantidad_base': Decimal('1'),
                'unidad_producto': 'PZA',
                'activo': True,
                'creado_por': admin,
            },
        )
        bom.tipo = BOM.TipoBOM.MFG
        bom.activo = True
        bom.save(update_fields=['tipo', 'activo'])

        for sku, qty in [('MAT-0001', '1.500'), ('MAT-0008', '12.000'), ('MAT-0014', '1.000'), ('MAT-0036', '1.000')]:
            BOMDetalle.objects.get_or_create(
                bom=bom,
                material=materiales[sku],
                defaults={'cantidad': Decimal(qty), 'observaciones': self.marker},
            )

        BOMOperacion.objects.get_or_create(
            bom=bom,
            secuencia=1,
            defaults={
                'nombre': 'Ensamble y validación funcional',
                'descripcion': self.marker,
                'linea_produccion': 'Línea Ensamble A',
                'tiempo_estimado': Decimal('18'),
                'unidad_tiempo': BOMOperacion.UnidadTiempo.MINUTOS,
                'recurso_maquina': 'Banco de prueba funcional',
                'operadores_requeridos': 2,
            },
        )

        return {'almacenes': almacenes, 'materiales': materiales, 'proveedores': proveedores, 'clientes': clientes, 'bom': bom}

    def seed_inventory(self, users, catalogs):
        today = date.today()
        dates = [today - timedelta(days=offset) for offset in range(4, -1, -1)]
        materiales = catalogs['materiales']
        almacenes = catalogs['almacenes']
        proveedores = catalogs['proveedores']
        user = users['inventario']
        count = 0

        entries = [
            (dates[0], 'Aceros Industriales del Norte', 'MAT-0001', 'L-260629-A', 'MP', 520),
            (dates[1], 'Fasteners Plus', 'MAT-0008', 'L-260630-F', 'MP', 7200),
            (dates[2], 'Arneses Integrados SA', 'MAT-0014', 'L-260701-A', 'QA', 360),
            (dates[3], 'PCB Solutions México', 'MAT-0036', 'L-260702-PCB', 'QA', 260),
            (dates[4], 'Polímeros del Bajío', 'MAT-0004', 'L-260703-P', 'MP', 210),
        ]
        for idx, (fecha, proveedor, sku, lote, almacen, qty) in enumerate(entries, start=1):
            recepcion, created = RecepcionMaterial.objects.get_or_create(
                orden_compra=f'DEMO-OC-INV-{idx:02d}',
                defaults={
                    'fecha_recepcion': fecha,
                    'hora_recepcion': time(8 + idx, 10),
                    'proveedor': proveedor,
                    'proveedor_registrado': proveedores[proveedor],
                    'factura': f'DEMO-FAC-{idx:02d}',
                    'transportista': 'Transportes Demo MX',
                    'placas': f'DEM-{idx:03d}',
                    'chk_oc': True,
                    'chk_cantidad': True,
                    'chk_empaque': True,
                    'chk_lote': True,
                    'chk_vigencia': True,
                    'chk_certificado': True,
                    'chk_estado_fisico': True,
                    'chk_foto': True,
                    'chk_calidad': almacen != 'QA',
                    'observaciones': self.marker,
                    'accion_recomendada': RecepcionMaterial.AccionRecomendada.ACEPTAR_TODO,
                    'estado': RecepcionMaterial.EstadoRecepcion.ENVIADA,
                    'creado_por': user,
                },
            )
            if created:
                RecepcionMaterialDetalle.objects.create(
                    recepcion=recepcion,
                    material=materiales[sku],
                    sku=sku,
                    descripcion=materiales[sku].nombre,
                    um=materiales[sku].um,
                    cantidad_oc=Decimal(str(qty)),
                    cantidad_recibida=Decimal(str(qty)),
                    lote=lote,
                    ubicacion_destino=almacen,
                    estatus=RecepcionMaterialDetalle.EstatusDetalle.ACEPTADO,
                )
                self.add_stock(materiales[sku], almacenes[almacen], lote, qty)
                count += 1

        salidas = [
            (dates[2], 'MAT-0001', 'L-260629-A', 140, 'Línea Ensamble A'),
            (dates[3], 'MAT-0008', 'L-260630-F', 2100, 'Línea Ensamble A'),
            (dates[4], 'MAT-0004', 'L-260703-P', 55, 'Línea Inyección'),
        ]
        for idx, (fecha, sku, lote, qty, linea) in enumerate(salidas, start=1):
            salida, created = SalidaLinea.objects.get_or_create(
                orden_produccion=f'DEMO-OF-SAL-{idx:02d}',
                defaults={
                    'fecha_salida': fecha,
                    'hora_salida': time(13, 15),
                    'linea_destino': linea,
                    'turno': 'Matutino',
                    'observaciones': self.marker,
                    'creado_por': user,
                },
            )
            if created:
                SalidaLineaDetalle.objects.create(
                    salida=salida,
                    almacen_origen=almacenes['MP'],
                    material=materiales[sku],
                    sku=sku,
                    descripcion=materiales[sku].nombre,
                    um=materiales[sku].um,
                    cantidad_enviada=Decimal(str(qty)),
                    lote=lote,
                )
                self.remove_stock(materiales[sku], almacenes['MP'], lote, qty)
                count += 1

        for idx, (sku, lote, qty) in enumerate([('MAT-0014', 'L-260701-A', 320), ('MAT-0036', 'L-260702-PCB', 230)], start=1):
            transferencia, created = TransferenciaAlmacen.objects.get_or_create(
                referencia=f'DEMO-TRF-QA-{idx:02d}',
                defaults={
                    'fecha_transferencia': dates[4],
                    'hora_transferencia': time(15, 30),
                    'almacen_origen': almacenes['QA'],
                    'almacen_destino': almacenes['MP'],
                    'motivo': self.marker,
                    'creado_por': user,
                },
            )
            if created:
                TransferenciaAlmacenDetalle.objects.create(
                    transferencia=transferencia,
                    material=materiales[sku],
                    sku=sku,
                    descripcion=materiales[sku].nombre,
                    um=materiales[sku].um,
                    cantidad_transferida=Decimal(str(qty)),
                    lote=lote,
                )
                self.remove_stock(materiales[sku], almacenes['QA'], lote, qty)
                self.add_stock(materiales[sku], almacenes['MP'], lote, qty)
                count += 1

        self.refresh_material_stock()
        return count

    def seed_production(self, users, catalogs):
        today = date.today()
        bom = catalogs['bom']
        materiales = catalogs['materiales']
        user = users['produccion']
        now = timezone.now()

        plan, _ = PlanProduccion.objects.get_or_create(
            folio='DEMO-PLAN-2607-01',
            defaults={
                'bom': bom,
                'cantidad_planificada': Decimal('450'),
                'fecha_inicio': today - timedelta(days=3),
                'fecha_fin': today + timedelta(days=2),
                'linea_produccion': 'Línea Ensamble A',
                'turno': 'Matutino',
                'observaciones': self.marker,
                'estado': PlanProduccion.EstadoPlan.EN_PROCESO,
                'creado_por': user,
            },
        )
        for material in [materiales['MAT-0001'], materiales['MAT-0008'], materiales['MAT-0014'], materiales['MAT-0036']]:
            PlanProduccionDetalle.objects.get_or_create(
                plan=plan,
                material=material,
                defaults={
                    'cantidad_requerida': Decimal('100'),
                    'cantidad_disponible': material.stock_actual,
                    'cantidad_faltante': Decimal('0'),
                    'estado_material': PlanProduccionDetalle.EstadoMaterial.DISPONIBLE,
                },
            )

        req, _ = RequerimientoMaterialProduccion.objects.get_or_create(
            folio='DEMO-REQ-MFG-01',
            defaults={
                'bom': bom,
                'cantidad_planificada': Decimal('450'),
                'notas': self.marker,
                'estado': RequerimientoMaterialProduccion.EstadoRequerimiento.BORRADOR,
                'creado_por': user,
            },
        )
        RequerimientoMaterialProduccionDetalle.objects.get_or_create(
            requerimiento=req,
            material=materiales['MAT-0036'],
            defaults={
                'cantidad_base_requerida': Decimal('450'),
                'cantidad_con_scrap': Decimal('468'),
                'stock_actual': materiales['MAT-0036'].stock_actual,
                'cantidad_sugerida_compra': Decimal('120'),
                'cantidad_solicitada': Decimal('120'),
                'observaciones': self.marker,
            },
        )

        ofs = []
        for idx, estado, plan_qty, prod_qty in [
            (1, OrdenFabricacion.EstadoOF.COMPLETADA, '180', '174'),
            (2, OrdenFabricacion.EstadoOF.EN_PROCESO, '150', '90'),
            (3, OrdenFabricacion.EstadoOF.COMPLETADA, '120', '118'),
        ]:
            of, _ = OrdenFabricacion.objects.get_or_create(
                folio=f'DEMO-OF-2607-{idx:02d}',
                defaults={
                    'plan': plan,
                    'bom': bom,
                    'cantidad_planificada': Decimal(plan_qty),
                    'cantidad_producida': Decimal(prod_qty),
                    'linea_produccion': 'Línea Ensamble A',
                    'turno': 'Matutino',
                    'estado': estado,
                    'fecha_inicio_programada': today - timedelta(days=idx),
                    'fecha_fin_programada': today + timedelta(days=1),
                    'fecha_inicio_real': now - timedelta(days=idx, hours=4),
                    'fecha_fin_real': now - timedelta(days=idx - 1) if estado == OrdenFabricacion.EstadoOF.COMPLETADA else None,
                    'observaciones': self.marker,
                    'creado_por': user,
                },
            )
            ofs.append(of)
            for material in [materiales['MAT-0001'], materiales['MAT-0008'], materiales['MAT-0014'], materiales['MAT-0036']]:
                OrdenFabricacion.objects.filter(pk=of.pk).update(fecha_actualizacion=now)
                of.detalles.get_or_create(
                    material=material,
                    defaults={
                        'cantidad_requerida': Decimal('80'),
                        'cantidad_consumida': Decimal('76'),
                        'observaciones': self.marker,
                    },
                )

        lote_validado, _ = LoteProduccion.objects.get_or_create(
            folio='DEMO-LOTE-2607-OK',
            defaults={
                'bom': bom,
                'orden_fabricacion': ofs[0],
                'cliente_destino': catalogs['clientes']['CLI-MAB'],
                'fecha_captura': today - timedelta(days=1),
                'hora_captura': time(16, 0),
                'linea_produccion': 'Línea Ensamble A',
                'turno': 'Matutino',
                'cantidad_producida': Decimal('174'),
                'operador': 'Equipo A',
                'estado': LoteProduccion.EstadoLote.VALIDADO,
                'observaciones': self.marker,
                'creado_por': user,
            },
        )
        lote_pendiente, _ = LoteProduccion.objects.get_or_create(
            folio='DEMO-LOTE-2607-PEND',
            defaults={
                'bom': bom,
                'orden_fabricacion': ofs[1],
                'cliente_destino': catalogs['clientes']['CLI-SAM'],
                'fecha_captura': today,
                'hora_captura': time(11, 0),
                'linea_produccion': 'Línea Ensamble A',
                'turno': 'Matutino',
                'cantidad_producida': Decimal('90'),
                'operador': 'Equipo B',
                'estado': LoteProduccion.EstadoLote.CAPTURADO,
                'observaciones': self.marker,
                'creado_por': user,
            },
        )
        return {'plan': plan, 'ofs': len(ofs), 'of_list': ofs, 'lotes': [lote_validado, lote_pendiente], 'req': req}

    def seed_finance(self, users, catalogs, production):
        today = date.today()
        user = users['finanzas']
        proveedor = catalogs['proveedores']['PCB Solutions México']
        material = catalogs['materiales']['MAT-0036']

        oc, _ = OrdenCompra.objects.get_or_create(
            folio='DEMO-OC-FIN-2607-01',
            defaults={
                'proveedor': proveedor,
                'requerimiento_origen': production['req'],
                'fecha_orden': today,
                'fecha_prometida': today + timedelta(days=8),
                'tiempo_entrega_estimado_dias': 8,
                'condiciones_pago': '30 días',
                'observaciones': self.marker,
                'estado': OrdenCompra.EstadoOrden.ENVIADA,
                'creada_desde_mfg': True,
                'total_estimado': Decimal('186000.00'),
                'creado_por': user,
            },
        )
        OrdenCompraDetalle.objects.get_or_create(
            orden=oc,
            material=material,
            defaults={
                'sku': material.sku,
                'descripcion': material.nombre,
                'um': material.um,
                'cantidad_pedida': Decimal('120'),
                'precio_unitario': Decimal('1550.00'),
                'subtotal': Decimal('186000.00'),
            },
        )

        for codigo, nombre, tipo in [
            ('1100', 'Bancos', CuentaContable.TipoCuenta.ACTIVO),
            ('4100', 'Ventas producto terminado', CuentaContable.TipoCuenta.INGRESO),
            ('5100', 'Costo de producción', CuentaContable.TipoCuenta.GASTO),
        ]:
            CuentaContable.objects.get_or_create(
                codigo=codigo,
                defaults={'nombre': nombre, 'tipo': tipo, 'descripcion': self.marker, 'creado_por': user, 'actualizado_por': user},
            )

        poliza, _ = PolizaContable.objects.get_or_create(
            folio='DEMO-POL-2607-01',
            defaults={
                'fecha_poliza': today,
                'tipo': PolizaContable.TipoPoliza.INGRESO,
                'concepto': 'Venta semanal demo de producto terminado',
                'referencia': self.marker,
                'estado': PolizaContable.EstadoPoliza.CONTABILIZADA,
                'creado_por': user,
                'actualizado_por': user,
            },
        )
        MovimientoContable = PolizaContable._meta.apps.get_model('autenticacion', 'MovimientoContable')
        MovimientoContable.objects.get_or_create(
            poliza=poliza,
            cuenta=CuentaContable.objects.get(codigo='1100'),
            descripcion='Cobro parcial cliente',
            defaults={'debe': Decimal('260000.00'), 'haber': Decimal('0')},
        )
        MovimientoContable.objects.get_or_create(
            poliza=poliza,
            cuenta=CuentaContable.objects.get(codigo='4100'),
            descripcion='Ingreso por venta',
            defaults={'debe': Decimal('0'), 'haber': Decimal('260000.00')},
        )

        PresupuestoFinanciero.objects.get_or_create(
            nombre='Presupuesto semanal producción',
            fecha_inicio=today - timedelta(days=6),
            fecha_fin=today + timedelta(days=24),
            defaults={
                'categoria': PresupuestoFinanciero.Categoria.OPEX,
                'periodicidad': PresupuestoFinanciero.Periodicidad.MENSUAL,
                'monto_presupuestado': Decimal('420000.00'),
                'monto_real': Decimal('286500.00'),
                'descripcion': self.marker,
                'activo': True,
                'creado_por': user,
                'actualizado_por': user,
            },
        )
        PresupuestoFinanciero.objects.get_or_create(
            nombre='Ingreso presupuestado semanal',
            fecha_inicio=today - timedelta(days=6),
            fecha_fin=today + timedelta(days=24),
            defaults={
                'categoria': PresupuestoFinanciero.Categoria.INGRESO,
                'periodicidad': PresupuestoFinanciero.Periodicidad.MENSUAL,
                'monto_presupuestado': Decimal('760000.00'),
                'monto_real': Decimal('520000.00'),
                'descripcion': self.marker,
                'activo': True,
                'creado_por': user,
                'actualizado_por': user,
            },
        )
        for folio, tipo, total, pagado, tercero in [
            ('DEMO-CXC-2607-01', CuentaPorPagarCobrar.TipoCuenta.POR_COBRAR, '520000.00', '260000.00', 'Mabe'),
            ('DEMO-CXP-2607-01', CuentaPorPagarCobrar.TipoCuenta.POR_PAGAR, '186000.00', '72000.00', proveedor.nombre),
        ]:
            CuentaPorPagarCobrar.objects.get_or_create(
                folio=folio,
                defaults={
                    'tipo': tipo,
                    'tercero_nombre': tercero,
                    'proveedor': proveedor if tipo == CuentaPorPagarCobrar.TipoCuenta.POR_PAGAR else None,
                    'cliente_compra': catalogs['clientes']['CLI-MAB'] if tipo == CuentaPorPagarCobrar.TipoCuenta.POR_COBRAR else None,
                    'orden_compra': oc if tipo == CuentaPorPagarCobrar.TipoCuenta.POR_PAGAR else None,
                    'monto_total': Decimal(total),
                    'monto_pagado': Decimal(pagado),
                    'fecha_emision': today,
                    'fecha_vencimiento': today + timedelta(days=20),
                    'estado': CuentaPorPagarCobrar.EstadoCuenta.PARCIAL,
                    'observaciones': self.marker,
                    'creado_por': user,
                    'actualizado_por': user,
                },
            )

        for idx, of in enumerate(production['of_list'], start=1):
            CosteoProduccion.objects.get_or_create(
                orden_fabricacion=of,
                lote_produccion=None,
                defaults={
                    'costo_material_plan': Decimal('78000.00'),
                    'costo_material_real': Decimal('80500.00'),
                    'costo_maquina_real': Decimal('12600.00'),
                    'costo_operador_real': Decimal('18200.00'),
                    'costo_total_plan': Decimal('108800.00'),
                    'costo_total_real': Decimal('111300.00'),
                    'ingreso_estimado': Decimal('168000.00'),
                    'rentabilidad': Decimal('56700.00'),
                    'margen_pct': Decimal('33.75'),
                    'estado': CosteoProduccion.EstadoCosteo.CERRADO,
                    'detalle': {'marker': self.marker, 'of': of.folio},
                    'creado_por': user,
                    'actualizado_por': user,
                },
            )

        DeclaracionImpuesto.objects.get_or_create(
            folio='DEMO-IVA-2607-01',
            defaults={
                'tipo_impuesto': DeclaracionImpuesto.TipoImpuesto.IVA,
                'periodo_inicio': today - timedelta(days=6),
                'periodo_fin': today,
                'base_gravable': Decimal('520000.00'),
                'tasa': Decimal('16.00'),
                'impuesto_calculado': Decimal('83200.00'),
                'estado': DeclaracionImpuesto.EstadoDeclaracion.CALCULADA,
                'acuse': self.marker,
                'detalle': {'marker': self.marker},
                'creado_por': user,
                'actualizado_por': user,
            },
        )
        return 8

    def seed_quality(self, users, catalogs, production):
        user = users['qa']
        today = date.today()
        lote = production['lotes'][0]
        reclamo, _ = ReclamoCliente.objects.get_or_create(
            folio='DEMO-RC-2607-01',
            defaults={
                'cliente': 'Samsung Electronics Mexico',
                'cliente_compra': catalogs['clientes']['CLI-SAM'],
                'producto_lote': lote.folio,
                'tipo_reclamo': ReclamoCliente.TipoReclamo.FUNCIONAL,
                'estado_reclamo': ReclamoCliente.EstadoReclamo.EN_ANALISIS,
                'prioridad': ReclamoCliente.PrioridadReclamo.ALTA,
                'descripcion': self.marker,
                'creado_por': user,
            },
        )
        scrap, _ = RegistroScrapDefecto.objects.get_or_create(
            lote=lote,
            tipo_defecto=RegistroScrapDefecto.TipoDefecto.FUNCIONAL,
            causa='Prueba funcional intermitente',
            defaults={
                'orden': production['of_list'][0],
                'cantidad_defectos': Decimal('6'),
                'descripcion': self.marker,
                'registrado_por': users['produccion'],
                'actualizado_por': users['produccion'],
            },
        )
        InformeValidacionDefectoQA.objects.get_or_create(
            defecto=scrap,
            defaults={
                'resultado_validacion': InformeValidacionDefectoQA.ResultadoValidacion.VALIDADO,
                'falla_maquina': False,
                'informe': f'{self.marker} | Defecto validado por QA.',
                'acciones_contencion': 'Inspección 100% por lote y ajuste de prueba funcional.',
                'validado_por': user,
            },
        )
        return 3 if reclamo else 2

    def seed_it(self, users, catalogs):
        now = timezone.now()
        it = users['it']
        TicketSoporte.objects.get_or_create(
            folio='TCK-DEMO-2607-01',
            defaults={
                'solicitado_por': users['finanzas'],
                'titulo': 'Acceso a reporte financiero semanal',
                'descripcion': self.marker,
                'prioridad': TicketSoporte.Prioridad.ALTA,
                'estado': TicketSoporte.Estado.EN_PROCESO,
                'asignado_a': it,
                'respuesta': 'Validando permisos de Finanzas.',
            },
        )
        for idx, username in enumerate(['admin2', 'finanzas2', 'inventario2', 'produccion2', 'qa2'], start=1):
            BitacoraAcceso.objects.get_or_create(
                usuario_ingresado=username,
                accion='login',
                fecha=now - timedelta(hours=idx),
                defaults={
                    'usuario': users.get(username.replace('2', ''), None),
                    'exitoso': True,
                    'ip': f'10.0.0.{idx}',
                    'user_agent': f'{self.marker} | Safari/Chrome demo',
                },
            )
        BitacoraAcceso.objects.get_or_create(
            usuario_ingresado='usuario.incorrecto',
            accion='login',
            fecha=now,
            defaults={'exitoso': False, 'ip': '10.0.0.99', 'user_agent': self.marker},
        )
        HistorialCambioUsuario.objects.get_or_create(
            usuario_afectado=users['finanzas'],
            realizado_por=it,
            accion='Actualización de acceso',
            defaults={'detalle': f'{self.marker} | Se habilitó acceso a dashboard financiero.'},
        )
        return 8

    def add_stock(self, material, almacen, lote, cantidad):
        inv, _ = InventarioAlmacen.objects.get_or_create(
            material=material,
            almacen=almacen,
            lote=lote,
            defaults={'stock_actual': Decimal('0')},
        )
        inv.stock_actual = (inv.stock_actual or Decimal('0')) + Decimal(str(cantidad))
        inv.save(update_fields=['stock_actual'])

    def remove_stock(self, material, almacen, lote, cantidad):
        inv, _ = InventarioAlmacen.objects.get_or_create(
            material=material,
            almacen=almacen,
            lote=lote,
            defaults={'stock_actual': Decimal('0')},
        )
        inv.stock_actual = max(Decimal('0'), (inv.stock_actual or Decimal('0')) - Decimal(str(cantidad)))
        inv.save(update_fields=['stock_actual'])

    def refresh_material_stock(self):
        for material in Material.objects.all():
            material.stock_actual = sum((item.stock_actual or Decimal('0')) for item in material.inventarios_almacen.all())
            material.save(update_fields=['stock_actual'])
