from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import (
    Almacen,
    BitacoraAcceso,
    BOM,
    BOMDetalle,
    ClienteCompra,
    CuentaContable,
    CuentaPorPagarCobrar,
    CostoHoraMaquina,
    CostoHoraOperador,
    CosteoProduccion,
    DeclaracionImpuesto,
    Departamento,
    EstadoFinanciero,
    HistorialCambioUsuario,
    InformeValidacionDefectoQA,
    InventarioAlmacen,
    Material,
    MovimientoContable,
    OrdenCompra,
    OrdenCompraDetalle,
    PolizaContable,
    PresupuestoFinanciero,
    Proveedor,
    ProveedorMaterialPrecio,
    ReporteKPIProduccion,
    ReporteFinanciero,
    RegistroScrapDefecto,
    RegistroUsoRecursoProduccion,
    SalidaLinea,
    SalidaLineaDetalle,
    TransferenciaAlmacen,
    TransferenciaAlmacenDetalle,
    TicketSoporte,
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


@admin.register(BitacoraAcceso)
class BitacoraAccesoAdmin(admin.ModelAdmin):
    list_display = ('fecha', 'usuario', 'usuario_ingresado', 'accion', 'exitoso', 'ip')
    search_fields = ('usuario__username', 'usuario_ingresado', 'ip')
    list_filter = ('accion', 'exitoso', 'fecha')
    readonly_fields = ('fecha',)


@admin.register(HistorialCambioUsuario)
class HistorialCambioUsuarioAdmin(admin.ModelAdmin):
    list_display = ('fecha', 'usuario_afectado', 'realizado_por', 'accion')
    search_fields = ('usuario_afectado__username', 'realizado_por__username', 'accion', 'detalle')
    list_filter = ('accion', 'fecha')
    readonly_fields = ('fecha',)


@admin.register(TicketSoporte)
class TicketSoporteAdmin(admin.ModelAdmin):
    list_display = ('folio', 'titulo', 'solicitado_por', 'asignado_a', 'prioridad', 'estado', 'fecha_actualizacion')
    search_fields = ('folio', 'titulo', 'descripcion', 'solicitado_por__username')
    list_filter = ('estado', 'prioridad', 'fecha_creacion')


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


@admin.register(BOM)
class BOMAdmin(admin.ModelAdmin):
    list_display = ('codigo', 'producto', 'version', 'cantidad_base', 'unidad_producto', 'activo', 'creado_por')
    search_fields = ('codigo', 'producto', 'descripcion')
    list_filter = ('activo', 'version')


@admin.register(BOMDetalle)
class BOMDetalleAdmin(admin.ModelAdmin):
    list_display = ('bom', 'material', 'cantidad', 'observaciones')
    search_fields = ('bom__codigo', 'bom__producto', 'material__sku', 'material__nombre')
    list_filter = ('bom',)


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


@admin.register(ClienteCompra)
class ClienteCompraAdmin(admin.ModelAdmin):
    list_display = ('codigo', 'nombre', 'contacto', 'email', 'telefono', 'activo')
    search_fields = ('codigo', 'nombre', 'contacto', 'email', 'telefono')
    list_filter = ('activo',)


@admin.register(ReporteKPIProduccion)
class ReporteKPIProduccionAdmin(admin.ModelAdmin):
    list_display = ('fecha_inicio', 'fecha_fin', 'oee', 'cumplimiento_ordenes', 'tasa_rechazo', 'fecha_generacion')
    search_fields = ('fecha_inicio', 'fecha_fin', 'generado_por__username')
    list_filter = ('fecha_inicio', 'fecha_fin', 'fecha_generacion')


@admin.register(CostoHoraMaquina)
class CostoHoraMaquinaAdmin(admin.ModelAdmin):
    list_display = ('linea_produccion', 'maquina_nombre', 'costo_hora', 'activo', 'actualizado_por', 'fecha_actualizacion')
    search_fields = ('linea_produccion', 'maquina_nombre', 'notas')
    list_filter = ('linea_produccion', 'activo')


@admin.register(CostoHoraOperador)
class CostoHoraOperadorAdmin(admin.ModelAdmin):
    list_display = ('operador', 'nomina_hora', 'costo_hora_real', 'activo', 'actualizado_por', 'fecha_actualizacion')
    search_fields = ('operador__username', 'operador__first_name', 'operador__last_name', 'notas')
    list_filter = ('activo', 'operador__departamento')


@admin.register(RegistroUsoRecursoProduccion)
class RegistroUsoRecursoProduccionAdmin(admin.ModelAdmin):
    list_display = ('orden', 'tipo_recurso', 'horas_reales', 'costo_total', 'registrado_por', 'fecha_creacion')
    search_fields = ('orden__folio', 'notas', 'costo_maquina__maquina_nombre', 'costo_operador__operador__username')
    list_filter = ('tipo_recurso', 'fecha_creacion')


@admin.register(RegistroScrapDefecto)
class RegistroScrapDefectoAdmin(admin.ModelAdmin):
    list_display = ('orden', 'lote', 'tipo_defecto', 'cantidad_defectos', 'causa', 'registrado_por', 'fecha_creacion')
    search_fields = ('orden__folio', 'lote__folio', 'causa', 'descripcion')
    list_filter = ('tipo_defecto', 'fecha_creacion')


@admin.register(InformeValidacionDefectoQA)
class InformeValidacionDefectoQAAdmin(admin.ModelAdmin):
    list_display = ('defecto', 'resultado_validacion', 'falla_maquina', 'validado_por', 'fecha_actualizacion')
    search_fields = ('defecto__orden__folio', 'defecto__lote__folio', 'informe', 'acciones_contencion')
    list_filter = ('resultado_validacion', 'falla_maquina', 'fecha_actualizacion')


@admin.register(CuentaContable)
class CuentaContableAdmin(admin.ModelAdmin):
    list_display = ('codigo', 'nombre', 'tipo', 'activa', 'actualizado_por', 'fecha_actualizacion')
    search_fields = ('codigo', 'nombre', 'descripcion')
    list_filter = ('tipo', 'activa')


@admin.register(PolizaContable)
class PolizaContableAdmin(admin.ModelAdmin):
    list_display = ('folio', 'fecha_poliza', 'tipo', 'concepto', 'estado', 'actualizado_por')
    search_fields = ('folio', 'concepto', 'referencia')
    list_filter = ('tipo', 'estado', 'fecha_poliza')


@admin.register(MovimientoContable)
class MovimientoContableAdmin(admin.ModelAdmin):
    list_display = ('poliza', 'cuenta', 'debe', 'haber')
    search_fields = ('poliza__folio', 'cuenta__codigo', 'cuenta__nombre', 'descripcion')
    list_filter = ('cuenta__tipo',)


@admin.register(EstadoFinanciero)
class EstadoFinancieroAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'tipo', 'fecha_inicio', 'fecha_fin', 'actualizado_por')
    search_fields = ('nombre', 'notas')
    list_filter = ('tipo', 'fecha_inicio', 'fecha_fin')


@admin.register(PresupuestoFinanciero)
class PresupuestoFinancieroAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'categoria', 'periodicidad', 'monto_presupuestado', 'monto_real', 'activo')
    search_fields = ('nombre', 'descripcion')
    list_filter = ('categoria', 'periodicidad', 'activo')


@admin.register(CuentaPorPagarCobrar)
class CuentaPorPagarCobrarAdmin(admin.ModelAdmin):
    list_display = ('folio', 'tipo', 'tercero_nombre', 'monto_total', 'monto_pagado', 'fecha_vencimiento', 'estado')
    search_fields = ('folio', 'tercero_nombre', 'observaciones')
    list_filter = ('tipo', 'estado', 'fecha_vencimiento')


@admin.register(CosteoProduccion)
class CosteoProduccionAdmin(admin.ModelAdmin):
    list_display = ('orden_fabricacion', 'lote_produccion', 'costo_total_plan', 'costo_total_real', 'rentabilidad', 'margen_pct', 'estado')
    search_fields = ('orden_fabricacion__folio', 'lote_produccion__folio')
    list_filter = ('estado', 'fecha_actualizacion')


@admin.register(ReporteFinanciero)
class ReporteFinancieroAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'tipo', 'fecha_inicio', 'fecha_fin', 'actualizado_por')
    search_fields = ('nombre',)
    list_filter = ('tipo', 'fecha_inicio', 'fecha_fin')


@admin.register(DeclaracionImpuesto)
class DeclaracionImpuestoAdmin(admin.ModelAdmin):
    list_display = ('folio', 'tipo_impuesto', 'periodo_inicio', 'periodo_fin', 'impuesto_calculado', 'estado')
    search_fields = ('folio', 'acuse')
    list_filter = ('tipo_impuesto', 'estado', 'periodo_inicio', 'periodo_fin')


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
