from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('autenticacion', '0015_transferenciaalmacen_transferenciaalmacendetalle'),
    ]

    operations = [
        migrations.CreateModel(
            name='OrdenCompra',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('folio', models.CharField(max_length=30, unique=True, verbose_name='Folio')),
                ('fecha_orden', models.DateField(verbose_name='Fecha de orden')),
                ('fecha_prometida', models.DateField(blank=True, null=True, verbose_name='Fecha prometida')),
                ('condiciones_pago', models.CharField(blank=True, max_length=120, verbose_name='Condiciones de pago')),
                ('observaciones', models.TextField(blank=True, verbose_name='Observaciones')),
                ('estado', models.CharField(choices=[('BORRADOR', 'Borrador'), ('APROBADA', 'Aprobada'), ('ENVIADA', 'Enviada'), ('PARCIAL', 'Parcial'), ('RECIBIDA', 'Recibida'), ('CANCELADA', 'Cancelada')], default='BORRADOR', max_length=12, verbose_name='Estado')),
                ('total_estimado', models.DecimalField(decimal_places=2, default=0, max_digits=14, verbose_name='Total estimado')),
                ('fecha_creacion', models.DateTimeField(auto_now_add=True)),
                ('creado_por', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='ordenes_compra', to='autenticacion.usuarioerp')),
                ('proveedor', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='ordenes_compra', to='autenticacion.proveedor')),
            ],
            options={
                'verbose_name': 'Orden de compra',
                'verbose_name_plural': 'Órdenes de compra',
                'ordering': ['-fecha_creacion'],
            },
        ),
        migrations.CreateModel(
            name='OrdenCompraDetalle',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('sku', models.CharField(max_length=50)),
                ('descripcion', models.CharField(max_length=255)),
                ('um', models.CharField(blank=True, max_length=20, verbose_name='Unidad de medida')),
                ('cantidad_pedida', models.DecimalField(decimal_places=2, default=0, max_digits=12, verbose_name='Cantidad pedida')),
                ('precio_unitario', models.DecimalField(decimal_places=2, default=0, max_digits=14, verbose_name='Precio unitario')),
                ('subtotal', models.DecimalField(decimal_places=2, default=0, max_digits=14, verbose_name='Subtotal')),
                ('material', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='ordenes_compra_detalle', to='autenticacion.material')),
                ('orden', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='detalles', to='autenticacion.ordencompra')),
            ],
            options={
                'verbose_name': 'Detalle orden de compra',
                'verbose_name_plural': 'Detalles orden de compra',
            },
        ),
    ]
