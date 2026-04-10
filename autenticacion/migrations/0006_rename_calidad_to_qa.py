from django.db import migrations


def rename_or_create_qa_department(apps, schema_editor):
    Departamento = apps.get_model('autenticacion', 'Departamento')

    calidad = Departamento.objects.filter(nombre='Calidad').first()
    qa = Departamento.objects.filter(nombre='QA').first()

    if calidad and not qa:
        calidad.nombre = 'QA'
        calidad.descripcion = 'Quality Assurance: SQA para entrada, OQA para salida y Customer Service para reclamos'
        calidad.save(update_fields=['nombre', 'descripcion'])
    elif not qa:
        Departamento.objects.create(
            nombre='QA',
            descripcion='Quality Assurance: SQA para entrada, OQA para salida y Customer Service para reclamos',
            activo=True,
        )


def reverse_rename_qa_department(apps, schema_editor):
    Departamento = apps.get_model('autenticacion', 'Departamento')
    qa = Departamento.objects.filter(nombre='QA').first()

    if qa and not Departamento.objects.filter(nombre='Calidad').exists():
        qa.nombre = 'Calidad'
        qa.descripcion = 'Inspección de material recibido y liberación para línea de producción'
        qa.save(update_fields=['nombre', 'descripcion'])


class Migration(migrations.Migration):

    dependencies = [
        ('autenticacion', '0004_alter_usuarioerp_options_usuarioerp_activo_and_more'),
    ]

    operations = [
        migrations.RunPython(rename_or_create_qa_department, reverse_rename_qa_department),
    ]
