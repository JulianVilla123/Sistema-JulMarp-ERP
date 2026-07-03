from datetime import date, time
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from autenticacion.models import (
    Almacen,
    InventarioAlmacen,
    Material,
    Proveedor,
    RecepcionMaterial,
    RecepcionMaterialDetalle,
    SalidaLinea,
    SalidaLineaDetalle,
    TransferenciaAlmacen,
    TransferenciaAlmacenDetalle,
)


class Command(BaseCommand):
    help = 'Crea datos demo de movimientos de almacén para la semana actual.'

    semana = [
        date(2026, 6, 29),
        date(2026, 6, 30),
        date(2026, 7, 1),
        date(2026, 7, 2),
        date(2026, 7, 3),
    ]
    marcador = 'DEMO_MOV_SEMANA_2026W27'

    def handle(self, *args, **options):
        with transaction.atomic():
            usuario = self.obtener_usuario()
            self.crear_catalogos_base()

            if RecepcionMaterial.objects.filter(observaciones__icontains=self.marcador).exists():
                self.stdout.write(self.style.WARNING('Los movimientos demo de esta semana ya existen. No se duplicó información.'))
                return

            resumen = self.crear_movimientos(usuario)
            self.stdout.write(self.style.SUCCESS(
                f"Demo creado: {resumen['entradas']} entradas, {resumen['salidas']} salidas, "
                f"{resumen['transferencias']} transferencias y {resumen['inventarios']} registros de inventario."
            ))

    def obtener_usuario(self):
        User = get_user_model()
        return (
            User.objects.filter(username='inventario2').first()
            or User.objects.filter(username='admin2').first()
            or User.objects.order_by('id').first()
        )

    def crear_catalogos_base(self):
        almacenes = [
            ('MP', 'Materia prima', 'Material recibido de proveedores'),
            ('QA', 'Cuarentena QA', 'Material pendiente de liberación de calidad'),
            ('WIP', 'Producción WIP', 'Material entregado a líneas de producción'),
            ('PT', 'Producto terminado', 'Material listo para embarque'),
        ]
        for codigo, nombre, descripcion in almacenes:
            Almacen.objects.get_or_create(
                codigo=codigo,
                defaults={'nombre': nombre, 'descripcion': descripcion, 'activo': True},
            )

        materiales = [
            ('MAT-0001', 'Lamina galvanizada 1mm', 'PZA'),
            ('MAT-0004', 'Resina ABS negra', 'SACO'),
            ('MAT-0008', 'Tornillo M4x20', 'PZA'),
            ('MAT-0014', 'Arnes electrico tipo A', 'PZA'),
            ('MAT-0021', 'Etiqueta codigo barras 50x30', 'ROLLO'),
            ('MAT-0036', 'Tarjeta PCB control', 'PZA'),
        ]
        for sku, nombre, um in materiales:
            Material.objects.get_or_create(
                sku=sku,
                defaults={'nombre': nombre, 'um': um, 'activo': True},
            )

        proveedores = [
            ('Aceros Industriales del Norte', 'ventas@acerosnorte.com'),
            ('Polímeros del Bajío', 'ventas@polimerosbajio.com'),
            ('Fasteners Plus', 'ventas@fastenersplus.com'),
            ('Arneses Integrados SA', 'ventas@arnesesintegrados.com'),
            ('Etiquetas y Códigos SA', 'contacto@etiquetasycodigos.com'),
            ('PCB Solutions México', 'ventas@pcbsolutions.com'),
        ]
        for nombre, email in proveedores:
            Proveedor.objects.get_or_create(
                nombre=nombre,
                defaults={'email': email, 'activo': True},
            )

    def crear_movimientos(self, usuario):
        materiales = {material.sku: material for material in Material.objects.filter(sku__in=[
            'MAT-0001', 'MAT-0004', 'MAT-0008', 'MAT-0014', 'MAT-0021', 'MAT-0036',
        ])}
        almacenes = {almacen.codigo: almacen for almacen in Almacen.objects.filter(codigo__in=['MP', 'QA', 'WIP', 'PT'])}

        entradas = [
            (self.semana[0], 'Aceros Industriales del Norte', 'OC-260629-001', 'MAT-0001', 'Lamina galvanizada 1mm', 'PZA', 'L-260629-A', 'MP', 480, 480, RecepcionMaterialDetalle.EstatusDetalle.ACEPTADO),
            (self.semana[0], 'Fasteners Plus', 'OC-260629-002', 'MAT-0008', 'Tornillo M4x20', 'PZA', 'L-260629-F', 'MP', 6000, 6000, RecepcionMaterialDetalle.EstatusDetalle.ACEPTADO),
            (self.semana[1], 'Polímeros del Bajío', 'OC-260630-001', 'MAT-0004', 'Resina ABS negra', 'SACO', 'L-260630-P', 'MP', 180, 175, RecepcionMaterialDetalle.EstatusDetalle.DIFERENCIA),
            (self.semana[1], 'Arneses Integrados SA', 'OC-260630-002', 'MAT-0014', 'Arnes electrico tipo A', 'PZA', 'L-260630-A', 'QA', 320, 320, RecepcionMaterialDetalle.EstatusDetalle.ACEPTADO),
            (self.semana[2], 'Etiquetas y Códigos SA', 'OC-260701-001', 'MAT-0021', 'Etiqueta codigo barras 50x30', 'ROLLO', 'L-260701-E', 'MP', 75, 75, RecepcionMaterialDetalle.EstatusDetalle.ACEPTADO),
            (self.semana[3], 'PCB Solutions México', 'OC-260702-001', 'MAT-0036', 'Tarjeta PCB control', 'PZA', 'L-260702-PCB', 'QA', 220, 216, RecepcionMaterialDetalle.EstatusDetalle.DIFERENCIA),
            (self.semana[4], 'Aceros Industriales del Norte', 'OC-260703-001', 'MAT-0001', 'Lamina galvanizada 1mm', 'PZA', 'L-260703-A', 'MP', 360, 360, RecepcionMaterialDetalle.EstatusDetalle.ACEPTADO),
        ]

        total_entradas = 0
        for index, entrada in enumerate(entradas, start=1):
            fecha, proveedor, oc, sku, descripcion, um, lote, almacen, cantidad_oc, cantidad_recibida, estatus = entrada
            recepcion = RecepcionMaterial.objects.create(
                fecha_recepcion=fecha,
                hora_recepcion=time(8 + index % 4, 15),
                proveedor=proveedor,
                proveedor_registrado=Proveedor.objects.filter(nombre=proveedor).first(),
                orden_compra=oc,
                factura=f'FAC-{oc[-6:]}',
                transportista='Transportes Norte MX',
                placas=f'DEM-{index:03d}',
                chk_oc=True,
                chk_cantidad=estatus == RecepcionMaterialDetalle.EstatusDetalle.ACEPTADO,
                chk_empaque=True,
                chk_lote=True,
                chk_vigencia=True,
                chk_certificado=True,
                chk_estado_fisico=True,
                chk_foto=True,
                chk_calidad=almacen != 'QA',
                observaciones=f'{self.marcador} | Recepción simulada para tesis.',
                accion_recomendada=RecepcionMaterial.AccionRecomendada.ACEPTAR_TODO if estatus == RecepcionMaterialDetalle.EstatusDetalle.ACEPTADO else RecepcionMaterial.AccionRecomendada.ACEPTAR_PARCIAL,
                estado=RecepcionMaterial.EstadoRecepcion.ENVIADA,
                creado_por=usuario,
            )
            material = materiales[sku]
            RecepcionMaterialDetalle.objects.create(
                recepcion=recepcion,
                material=material,
                sku=sku,
                descripcion=descripcion,
                um=um,
                cantidad_oc=Decimal(str(cantidad_oc)),
                cantidad_recibida=Decimal(str(cantidad_recibida)),
                lote=lote,
                ubicacion_destino=almacen,
                estatus=estatus,
            )
            if estatus == RecepcionMaterialDetalle.EstatusDetalle.ACEPTADO:
                self.sumar_inventario(material, almacenes[almacen], lote, cantidad_recibida)
                total_entradas += 1

        salidas = [
            (self.semana[1], 'Línea Ensamble A', 'OF-260630-A', 'MAT-0001', 'L-260629-A', 120),
            (self.semana[2], 'Línea Ensamble B', 'OF-260701-B', 'MAT-0008', 'L-260629-F', 1800),
            (self.semana[3], 'Línea Inyección', 'OF-260702-I', 'MAT-0004', 'L-260630-P', 40),
            (self.semana[4], 'Línea Ensamble A', 'OF-260703-A', 'MAT-0021', 'L-260701-E', 18),
        ]

        total_salidas = 0
        for index, salida_data in enumerate(salidas, start=1):
            fecha, linea, orden, sku, lote, cantidad = salida_data
            material = materiales[sku]
            salida = SalidaLinea.objects.create(
                fecha_salida=fecha,
                hora_salida=time(10 + index, 30),
                linea_destino=linea,
                orden_produccion=orden,
                turno='Matutino',
                observaciones=f'{self.marcador} | Surtido simulado a producción.',
                creado_por=usuario,
            )
            SalidaLineaDetalle.objects.create(
                salida=salida,
                almacen_origen=almacenes['MP'],
                material=material,
                sku=sku,
                descripcion=material.nombre,
                um=material.um,
                cantidad_enviada=Decimal(str(cantidad)),
                lote=lote,
            )
            self.restar_inventario(material, almacenes['MP'], lote, cantidad)
            total_salidas += 1

        transferencias = [
            (self.semana[3], 'QA', 'MP', 'MAT-0014', 'L-260630-A', 300, 'Liberación QA de arneses'),
            (self.semana[4], 'QA', 'MP', 'MAT-0036', 'L-260702-PCB', 200, 'Liberación parcial de PCB control'),
        ]
        total_transferencias = 0
        for fecha, origen, destino, sku, lote, cantidad, motivo in transferencias:
            material = materiales[sku]
            transferencia = TransferenciaAlmacen.objects.create(
                fecha_transferencia=fecha,
                hora_transferencia=time(14, 45),
                almacen_origen=almacenes[origen],
                almacen_destino=almacenes[destino],
                referencia=f'TRF-{fecha.strftime("%y%m%d")}-{sku[-2:]}',
                motivo=f'{self.marcador} | {motivo}',
                creado_por=usuario,
            )
            TransferenciaAlmacenDetalle.objects.create(
                transferencia=transferencia,
                material=material,
                sku=sku,
                descripcion=material.nombre,
                um=material.um,
                cantidad_transferida=Decimal(str(cantidad)),
                lote=lote,
            )
            self.restar_inventario(material, almacenes[origen], lote, cantidad)
            self.sumar_inventario(material, almacenes[destino], lote, cantidad)
            total_transferencias += 1

        self.actualizar_stock_materiales()

        return {
            'entradas': total_entradas,
            'salidas': total_salidas,
            'transferencias': total_transferencias,
            'inventarios': InventarioAlmacen.objects.count(),
        }

    def sumar_inventario(self, material, almacen, lote, cantidad):
        inventario, _ = InventarioAlmacen.objects.get_or_create(
            material=material,
            almacen=almacen,
            lote=lote,
            defaults={'stock_actual': Decimal('0')},
        )
        inventario.stock_actual = (inventario.stock_actual or Decimal('0')) + Decimal(str(cantidad))
        inventario.save(update_fields=['stock_actual'])

    def restar_inventario(self, material, almacen, lote, cantidad):
        inventario, _ = InventarioAlmacen.objects.get_or_create(
            material=material,
            almacen=almacen,
            lote=lote,
            defaults={'stock_actual': Decimal('0')},
        )
        inventario.stock_actual = max(Decimal('0'), (inventario.stock_actual or Decimal('0')) - Decimal(str(cantidad)))
        inventario.save(update_fields=['stock_actual'])

    def actualizar_stock_materiales(self):
        for material in Material.objects.all():
            total = sum(
                (item.stock_actual or Decimal('0'))
                for item in material.inventarios_almacen.all()
            )
            material.stock_actual = total
            material.save(update_fields=['stock_actual'])
