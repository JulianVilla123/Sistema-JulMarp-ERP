from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from autenticacion.models import FactorExternoPredictivo, UsuarioERP


class Command(BaseCommand):
    help = 'Registra factores externos controlados para validar la Fase 3 del modelo predictivo.'

    marker = 'DEMO_FASE3_FACTORES_EXTERNOS'

    def add_arguments(self, parser):
        parser.add_argument(
            '--reset',
            action='store_true',
            help='Elimina únicamente los factores de demostración de esta fase antes de recrearlos.',
        )

    def handle(self, *args, **options):
        usuario = UsuarioERP.objects.filter(is_superuser=True, activo=True).order_by('id').first()
        inicio = timezone.localdate() - timedelta(days=timezone.localdate().weekday())
        fin = inicio + timedelta(days=27)
        factores = [
            {
                'tipo': FactorExternoPredictivo.TipoFactor.PROMOCION,
                'nombre': 'Promoción de cierre mensual',
                'departamento': 'Finanzas',
                'metrica': 'ventas_clientes',
                'impacto_pct': Decimal('8.00'),
            },
            {
                'tipo': FactorExternoPredictivo.TipoFactor.TEMPORADA,
                'nombre': 'Temporada alta simulada',
                'departamento': 'MFG',
                'metrica': 'unidades_totales',
                'impacto_pct': Decimal('5.00'),
            },
            {
                'tipo': FactorExternoPredictivo.TipoFactor.RETRASO_PROVEEDOR,
                'nombre': 'Retraso de proveedor crítico',
                'departamento': 'Inventario',
                'metrica': 'demanda_material',
                'impacto_pct': Decimal('10.00'),
            },
            {
                'tipo': FactorExternoPredictivo.TipoFactor.FESTIVO,
                'nombre': 'Ajuste de personal por día festivo',
                'departamento': 'QA',
                'metrica': 'tasa_defecto',
                'impacto_pct': Decimal('12.00'),
            },
            {
                'tipo': FactorExternoPredictivo.TipoFactor.TEMPORADA,
                'nombre': 'Mayor carga operativa simulada',
                'departamento': 'Mantenimiento',
                'metrica': 'paros_previstos',
                'impacto_pct': Decimal('15.00'),
            },
        ]
        with transaction.atomic():
            if options['reset']:
                FactorExternoPredictivo.objects.filter(fuente=self.marker).delete()
            for factor in factores:
                FactorExternoPredictivo.objects.update_or_create(
                    fuente=self.marker,
                    nombre=factor['nombre'],
                    departamento=factor['departamento'],
                    metrica=factor['metrica'],
                    defaults={
                        **factor,
                        'fecha_inicio': inicio,
                        'fecha_fin': fin,
                        'fuente': self.marker,
                        'notas': 'Factor controlado para validación técnica de la Fase 3.',
                        'activo': True,
                        'creado_por': usuario,
                    },
                )
        self.stdout.write(self.style.SUCCESS(f'{len(factores)} factores externos registrados.'))
        self.stdout.write(f'Periodo de aplicación: {inicio} a {fin}.')
