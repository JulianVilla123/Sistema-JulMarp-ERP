from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import (
    Almacen,
    Departamento,
    InventarioAlmacen,
    Material,
    OrdenCompra,
    OrdenCompraDetalle,
    Proveedor,
    ProveedorMaterialPrecio,
    SalidaLinea,
    SalidaLineaDetalle,
    TransferenciaAlmacen,
    TransferenciaAlmacenDetalle,
    UsuarioERP,
)

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


@admin.register(ProveedorMaterialPrecio)
class ProveedorMaterialPrecioAdmin(admin.ModelAdmin):
    list_display = ('proveedor', 'material', 'precio_unitario', 'fecha_actualizacion')
    search_fields = ('proveedor__nombre', 'material__sku', 'material__nombre')
    list_filter = ('proveedor',)


@admin.register(Almacen)
class AlmacenAdmin(admin.ModelAdmin):
    list_display = ('codigo', 'nombre', 'activo')
    search_fields = ('codigo', 'nombre', 'descripcion')
    list_filter = ('activo',)


@admin.register(InventarioAlmacen)
class InventarioAlmacenAdmin(admin.ModelAdmin):
    list_display = ('almacen', 'material', 'lote', 'stock_actual', 'fecha_actualizacion')
    search_fields = ('almacen__codigo', 'almacen__nombre', 'material__sku', 'material__nombre', 'lote')
    list_filter = ('almacen',)


@admin.register(SalidaLinea)
class SalidaLineaAdmin(admin.ModelAdmin):
    list_display = ('id', 'fecha_salida', 'hora_salida', 'linea_destino', 'orden_produccion', 'creado_por')
    search_fields = ('linea_destino', 'orden_produccion', 'creado_por__username')
    list_filter = ('linea_destino',)


@admin.register(SalidaLineaDetalle)
class SalidaLineaDetalleAdmin(admin.ModelAdmin):
    list_display = ('salida', 'almacen_origen', 'material', 'lote', 'cantidad_enviada')
    search_fields = ('salida__id', 'almacen_origen__codigo', 'material__sku', 'descripcion', 'lote')
    list_filter = ('almacen_origen',)


@admin.register(TransferenciaAlmacen)
class TransferenciaAlmacenAdmin(admin.ModelAdmin):
    list_display = ('id', 'fecha_transferencia', 'hora_transferencia', 'almacen_origen', 'almacen_destino', 'referencia', 'creado_por')
    search_fields = ('almacen_origen__codigo', 'almacen_destino__codigo', 'referencia', 'creado_por__username')
    list_filter = ('almacen_origen', 'almacen_destino')


@admin.register(TransferenciaAlmacenDetalle)
class TransferenciaAlmacenDetalleAdmin(admin.ModelAdmin):
    list_display = ('transferencia', 'material', 'lote', 'cantidad_transferida')
    search_fields = ('transferencia__id', 'material__sku', 'descripcion', 'lote')
    list_filter = ('material',)


@admin.register(OrdenCompra)
class OrdenCompraAdmin(admin.ModelAdmin):
    list_display = ('folio', 'proveedor', 'fecha_orden', 'estado', 'total_estimado', 'creado_por')
    search_fields = ('folio', 'proveedor__nombre', 'creado_por__username')
    list_filter = ('estado', 'proveedor')


@admin.register(OrdenCompraDetalle)
class OrdenCompraDetalleAdmin(admin.ModelAdmin):
    list_display = ('orden', 'sku', 'descripcion', 'cantidad_pedida', 'precio_unitario', 'subtotal')
    search_fields = ('orden__folio', 'sku', 'descripcion')
    list_filter = ('orden__estado',)
