from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import Departamento, UsuarioERP

@admin.register(Departamento)
class DepartamentoAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'activo')
    search_fields = ('nombre',)
    list_filter = ('activo',)

@admin.register(UsuarioERP)
class UsuarioERPAdmin(UserAdmin):
    model = UsuarioERP
    fieldsets = UserAdmin.fieldsets + (
        ('Información adicional', {
            'fields': ('telefono', 'numero_empleado', 'departamento'),
        }),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ('Información adicional', {
            'fields': ('telefono', 'numero_empleado', 'departamento'),
        }),
    )
    list_display = ('username', 'email', 'first_name', 'last_name', 'departamento', 'is_staff')
    list_filter = ('departamento', 'is_staff', 'is_superuser', 'is_active')
    search_fields = ('username', 'email', 'first_name', 'last_name')
