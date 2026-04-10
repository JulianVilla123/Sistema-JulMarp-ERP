from django.core.management.base import BaseCommand
from autenticacion.models import Material, Proveedor


class Command(BaseCommand):
    help = 'Asigna materiales a proveedores de manera lógica según palabras clave'

    def handle(self, *args, **options):
        # Mapeo de palabras clave a tipos de materiales
        mapeo_proveedores = {
            'Aceros': ['lámina', 'galvanizada', 'hierro', 'acero', 'metal', 'placa'],
            'Polímeros': ['polímero', 'plástico', 'resina', 'abs', 'pp', 'pvc', 'saco'],
            'Fasteners': ['tornill', 'conector', 'tuerca', 'arandela', 'pernos', 'remache'],
            'Electrónic': ['cable', 'conductor', 'eléctrico', 'conector', 'sensor'],
            'Químico': ['pintura', 'adhesivo', 'lubricante', 'solvente', 'químico'],
            'Logística': ['caja', 'empaque', 'etiqueta', 'cartón', 'bolsa'],
            'Componentes': ['display', 'circuito', 'módulo', 'componente', 'dispositivo'],
            'Suministro': ['arandela', 'junta', 'sello', 'empaque', 'goma'],
        }

        # Obtener proveedores en la base de datos
        proveedores = Proveedor.objects.all()
        materiales = Material.objects.all()

        for proveedor in proveedores:
            materiales_asignados = []

            # Buscar palabras clave en el nombre del proveedor
            nombre_proveedor = proveedor.nombre.lower()

            for palabra_clave, palabras_material in mapeo_proveedores.items():
                if palabra_clave.lower() in nombre_proveedor:
                    # Si encontramos una coincidencia, asignar materiales relacionados
                    for material in materiales:
                        nombre_material = (material.nombre + ' ' + material.descripcion).lower()
                        for palabra_mat in palabras_material:
                            if palabra_mat in nombre_material:
                                if material not in materiales_asignados:
                                    materiales_asignados.append(material)
                                break

            # Si no encontramos coincidencias específicas, asignar algunos materiales aleatorios
            if not materiales_asignados:
                # Asignar aproximadamente 5-8 materiales aleatorios
                import random
                cantidad = random.randint(5, min(8, materiales.count()))
                materiales_asignados = list(random.sample(list(materiales), cantidad))

            # Asignar los materiales al proveedor
            proveedor.materiales.set(materiales_asignados)
            self.stdout.write(
                self.style.SUCCESS(
                    f'✓ Proveedor "{proveedor.nombre}" → {len(materiales_asignados)} materiales asignados'
                )
            )

        self.stdout.write(self.style.SUCCESS('\n✅ Asignación de materiales completada'))
