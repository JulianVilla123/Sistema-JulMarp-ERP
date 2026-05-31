from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('autenticacion', '0023_loteproduccion'),
    ]

    operations = [
        migrations.CreateModel(
            name='ReclamoCliente',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('folio', models.CharField(max_length=40, unique=True, verbose_name='Folio de reclamo')),
                ('cliente', models.CharField(max_length=200, verbose_name='Cliente')),
                ('producto_lote', models.CharField(blank=True, max_length=200, verbose_name='Producto / lote')),
                ('tipo_reclamo', models.CharField(choices=[('defecto_visual', 'Defecto visual'), ('funcional', 'Falla funcional'), ('documental', 'Error documental'), ('logistico', 'Incidencia logística')], max_length=30, verbose_name='Tipo de reclamo')),
                ('estado_reclamo', models.CharField(choices=[('abierto', 'Abierto'), ('en_analisis', 'En análisis'), ('en_contencion', 'En contención'), ('cerrado', 'Cerrado')], default='abierto', max_length=20, verbose_name='Estado del reclamo')),
                ('prioridad', models.CharField(choices=[('alta', 'Alta'), ('media', 'Media'), ('baja', 'Baja')], default='media', max_length=10, verbose_name='Prioridad')),
                ('descripcion', models.TextField(blank=True, verbose_name='Descripción')),
                ('fecha_creacion', models.DateTimeField(auto_now_add=True)),
                ('fecha_actualizacion', models.DateTimeField(auto_now=True)),
                ('creado_por', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='reclamos_cliente', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Reclamo de cliente',
                'verbose_name_plural': 'Reclamos de cliente',
                'ordering': ['-fecha_actualizacion', '-fecha_creacion'],
            },
        ),
    ]