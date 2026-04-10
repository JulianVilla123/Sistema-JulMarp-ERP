from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import Departamento, UsuarioERP, Material, Proveedor, Almacen, InventarioAlmacen

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
    list_display = (
        'username',
        'email',
        'numero_empleado',
        'first_name',
        'last_name',
        'departamento',
        'is_staff',
    )
    list_filter = ('departamento', 'is_staff', 'is_superuser', 'is_active')
    search_fields = ('username', 'email', 'numero_empleado', 'first_name', 'last_name')


@admin.register(Material)
class MaterialAdmin(admin.ModelAdmin):
    list_display = ('sku', 'nombre', 'proveedor_display', 'um', 'stock_actual', 'activo')
    search_fields = ('sku', 'nombre', 'descripcion', 'proveedor__nombre')
    list_filter = ('activo', 'um')

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        return queryset.prefetch_related('proveedor_set')

    def proveedor_display(self, obj):
        proveedor = next(iter(obj.proveedor_set.all()), None)
        return proveedor.nombre if proveedor else 'Sin proveedor'

    proveedor_display.short_description = 'Proveedor'


@admin.register(Proveedor)
class ProveedorAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'telefono', 'email', 'activo')
    search_fields = ('nombre', 'email', 'telefono')
    list_filter = ('activo',)
    filter_horizontal = ('materiales',)


@admin.register(Almacen)
class AlmacenAdmin(admin.ModelAdmin):
    list_display = ('codigo', 'nombre', 'activo')
    search_fields = ('codigo', 'nombre', 'descripcion')
    list_filter = ('activo',)


@admin.register(InventarioAlmacen)
class InventarioAlmacenAdmin(admin.ModelAdmin):
    list_display = ('almacen', 'material', 'stock_actual', 'fecha_actualizacion')
    search_fields = ('almacen__codigo', 'almacen__nombre', 'material__sku', 'material__nombre')
    list_filter = ('almacen',)
