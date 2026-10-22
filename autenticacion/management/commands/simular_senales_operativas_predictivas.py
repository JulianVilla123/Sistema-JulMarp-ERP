from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from autenticacion.models import (
    Material,
    OrdenCompra,
    OrdenCompraDetalle,
    Proveedor,
    UsuarioERP,
)


class Command(BaseCommand):
    help = 'Registra órdenes demo para probar precios y retrasos reales de proveedores.'

    marker = 'DEMO_FASE4_SENALES_OPERATIVAS'

    def add_arguments(self, parser):
        parser.add_argument('--reset', action='store_true')

    def handle(self, *args, **options):
        usuario = UsuarioERP.objects.filter(is_superuser=True, activo=True).order_by('id').first()
        if not usuario:
            raise CommandError('Se necesita un usuario ERP activo.')
        specs = [
            ('ML-MP-A', Decimal('1.90'), Decimal('2.20'), Decimal('120')),
            ('ML-MP-B', Decimal('2.10'), Decimal('2.50'), Decimal('160')),
            ('ML-MP-C', Decimal('1.80'), Decimal('2.10'), Decimal('140')),
        ]
        hoy = timezone.localdate()
        with transaction.atomic():
            if options['reset']:
                OrdenCompra.objects.filter(observaciones__icontains=self.marker).delete()
            for sku, precio_compra, _, cantidad in specs:
                material = Material.objects.filter(sku=sku).first()
                if not material:
                    raise CommandError(f'No existe el material simulado {sku}.')
                proveedor = Proveedor.objects.filter(materiales=material, activo=True).first()
                if not proveedor:
                    raise CommandError(f'No existe proveedor activo para {sku}.')
                folio = f'OC-DEMO-F4-{sku}'
                orden, _ = OrdenCompra.objects.update_or_create(
                    folio=folio,
                    defaults={
                        'proveedor': proveedor,
                        'fecha_orden': hoy - timedelta(days=14),
                        'fecha_prometida': hoy - timedelta(days=4),
                        'tiempo_entrega_estimado_dias': 7,
                        'estado': OrdenCompra.EstadoOrden.ENVIADA,
                        'creada_desde_mfg': True,
                        'total_estimado': precio_compra * cantidad,
                        'observaciones': f'{self.marker} | retraso simulado',
                        'creado_por': usuario,
                    },
                )
                OrdenCompraDetalle.objects.update_or_create(
                    orden=orden,
                    sku=sku,
                    defaults={
                        'material': material,
                        'descripcion': material.nombre,
                        'um': material.um,
                        'cantidad_pedida': cantidad,
                        'precio_unitario': precio_compra,
                        'subtotal': precio_compra * cantidad,
                    },
                )
        self.stdout.write(self.style.SUCCESS('Señales operativas de Fase 4 registradas.'))
        self.stdout.write('Se simularon precios de compra históricos y órdenes con retraso.')
