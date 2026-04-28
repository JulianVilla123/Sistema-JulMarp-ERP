from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('autenticacion', '0017_proveedormaterialprecio'),
    ]

    operations = [
        migrations.CreateModel(
            name='BOM',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('codigo', models.CharField(max_length=40, verbose_name='Código BOM')),
                ('producto', models.CharField(max_length=200, verbose_name='Producto')),
                ('version', models.CharField(default='1.0', max_length=20, verbose_name='Versión')),
                ('descripcion', models.TextField(blank=True, verbose_name='Descripción')),
                ('cantidad_base', models.DecimalField(decimal_places=2, default=1, max_digits=12, verbose_name='Cantidad base')),
                ('unidad_producto', models.CharField(blank=True, max_length=20, verbose_name='Unidad producto')),
                ('activo', models.BooleanField(default=True, verbose_name='Activo')),
                ('fecha_creacion', models.DateTimeField(auto_now_add=True)),
                ('fecha_actualizacion', models.DateTimeField(auto_now=True)),
                ('creado_por', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='boms_creados', to='autenticacion.usuarioerp')),
            ],
            options={
                'verbose_name': 'BOM',
                'verbose_name_plural': 'BOM',
                'ordering': ['producto', 'version'],
            },
        ),
        migrations.CreateModel(
            name='BOMDetalle',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('cantidad', models.DecimalField(decimal_places=3, max_digits=12, verbose_name='Cantidad requerida')),
                ('observaciones', models.CharField(blank=True, max_length=255, verbose_name='Observaciones')),
                ('bom', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='componentes', to='autenticacion.bom')),
                ('material', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='bom_detalles', to='autenticacion.material')),
            ],
            options={
                'verbose_name': 'Componente BOM',
                'verbose_name_plural': 'Componentes BOM',
                'ordering': ['material__sku'],
            },
        ),
        migrations.AddConstraint(
            model_name='bom',
            constraint=models.UniqueConstraint(fields=('codigo', 'version'), name='unique_bom_codigo_version'),
        ),
        migrations.AddConstraint(
            model_name='bomdetalle',
            constraint=models.UniqueConstraint(fields=('bom', 'material'), name='unique_bom_material'),
        ),
    ]