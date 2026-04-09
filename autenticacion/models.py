from django.db import models
from django.contrib.auth.models import AbstractUser

# Create your models here.

class Departamento(models.Model):
    nombre = models.CharField('Nombre del departamento', max_length=100, unique=True)
    descripcion = models.TextField('Descripción', blank=True)
    activo = models.BooleanField('Activo', default=True)

    def __str__(self):
        return self.nombre

    class Meta:
        verbose_name = 'Departamento'
        verbose_name_plural = 'Departamentos'


class UsuarioERP(AbstractUser):
    # Modelo de usuario personalizado para el ERP
    telefono = models.CharField('Teléfono', max_length=20, blank=True)
    numero_empleado = models.CharField('Número de empleado', max_length=30, blank=True)
    departamento = models.ForeignKey(
        Departamento,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name='Departamento'
    )

