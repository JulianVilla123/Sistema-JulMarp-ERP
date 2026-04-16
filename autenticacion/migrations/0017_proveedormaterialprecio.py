from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('autenticacion', '0016_ordencompra_ordencompradetalle'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProveedorMaterialPrecio',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('precio_unitario', models.DecimalField(decimal_places=2, default=0, max_digits=14, verbose_name='Precio unitario')),
                ('fecha_actualizacion', models.DateTimeField(auto_now=True)),
                ('material', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='precios_por_proveedor', to='autenticacion.material')),
                ('proveedor', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='precios_materiales', to='autenticacion.proveedor')),
            ],
            options={
                'verbose_name': 'Precio material por proveedor',
                'verbose_name_plural': 'Precios de materiales por proveedor',
            },
        ),
        migrations.AddConstraint(
            model_name='proveedormaterialprecio',
            constraint=models.UniqueConstraint(fields=('proveedor', 'material'), name='unique_proveedor_material_precio'),
        ),
    ]
