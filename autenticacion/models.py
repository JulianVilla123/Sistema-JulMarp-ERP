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
    numero_empleado = models.CharField('Número de empleado', max_length=30, blank=True, unique=True)
    departamento = models.ForeignKey(
        Departamento,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name='Departamento'
    )
    activo = models.BooleanField('Activo', default=True)
    fecha_creacion = models.DateTimeField('Fecha de creación', auto_now_add=True)
    fecha_ultima_sesion = models.DateTimeField('Última sesión', null=True, blank=True)

    @property
    def iniciales(self):
        nombre = (self.first_name or '').strip()
        apellido = (self.last_name or '').strip()

        primer_nombre = nombre.split()[0] if nombre else ''
        primer_apellido = apellido.split()[0] if apellido else ''

        inicial_nombre = primer_nombre[:1].upper()
        inicial_apellido = primer_apellido[:1].upper()

        if inicial_nombre and inicial_apellido:
            return f"{inicial_nombre}{inicial_apellido}"
        if inicial_nombre:
            return inicial_nombre
        if inicial_apellido:
            return inicial_apellido
        return (self.username or '')[:1].upper()

    def __str__(self):
        return f"{self.get_full_name() or self.username} - {self.departamento or 'Sin departamento'}"

    class Meta:
        verbose_name = 'Usuario ERP'
        verbose_name_plural = 'Usuarios ERP'


class RegistroAuditable(models.Model):
    creado_por = models.ForeignKey(
        'UsuarioERP',
        on_delete=models.PROTECT,
        related_name='%(class)s_creados',
    )
    actualizado_por = models.ForeignKey(
        'UsuarioERP',
        on_delete=models.PROTECT,
        related_name='%(class)s_actualizados',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_actualizacion = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Material(models.Model):
    sku = models.CharField('SKU', max_length=50, unique=True)
    nombre = models.CharField('Nombre', max_length=255)
    descripcion = models.TextField('Descripción', blank=True)
    um = models.CharField('Unidad de medida', max_length=20, blank=True)
    stock_actual = models.DecimalField('Stock actual', max_digits=14, decimal_places=2, default=0)
    activo = models.BooleanField('Activo', default=True)
    fecha_creacion = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    fecha_actualizacion = models.DateTimeField(auto_now=True, null=True, blank=True)

    def __str__(self):
        return f"{self.sku} - {self.nombre}"

    class Meta:
        verbose_name = 'Material'
        verbose_name_plural = 'Materiales'
        ordering = ['sku']


class BOM(models.Model):
    class TipoBOM(models.TextChoices):
        MATERIALES = 'MATERIALES', 'Materiales'
        MFG = 'MFG', 'Fabricación (MFG)'

    codigo = models.CharField('Código BOM', max_length=40)
    tipo = models.CharField(
        'Tipo',
        max_length=12,
        choices=TipoBOM.choices,
        default=TipoBOM.MATERIALES,
    )
    producto = models.CharField('Producto', max_length=200)
    version = models.CharField('Versión', max_length=20, default='1.0')
    descripcion = models.TextField('Descripción', blank=True)
    cantidad_base = models.DecimalField('Cantidad base', max_digits=12, decimal_places=2, default=1)
    unidad_producto = models.CharField('Unidad producto', max_length=20, blank=True)
    activo = models.BooleanField('Activo', default=True)
    creado_por = models.ForeignKey(
        UsuarioERP,
        on_delete=models.PROTECT,
        related_name='boms_creados',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_actualizacion = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.codigo} - {self.producto} v{self.version}"

    class Meta:
        verbose_name = 'BOM'
        verbose_name_plural = 'BOM'
        ordering = ['producto', 'version']
        constraints = [
            models.UniqueConstraint(
                fields=['codigo', 'version'],
                name='unique_bom_codigo_version',
            )
        ]


class BOMDetalle(models.Model):
    bom = models.ForeignKey(
        BOM,
        on_delete=models.CASCADE,
        related_name='componentes',
    )
    material = models.ForeignKey(
        Material,
        on_delete=models.PROTECT,
        related_name='bom_detalles',
    )
    cantidad = models.DecimalField('Cantidad requerida', max_digits=12, decimal_places=3)
    observaciones = models.CharField('Observaciones', max_length=255, blank=True)

    def __str__(self):
        return f"{self.bom.codigo} - {self.material.sku} ({self.cantidad})"

    class Meta:
        verbose_name = 'Componente BOM'
        verbose_name_plural = 'Componentes BOM'
        ordering = ['material__sku']
        constraints = [
            models.UniqueConstraint(
                fields=['bom', 'material'],
                name='unique_bom_material',
            )
        ]


class BOMOperacion(models.Model):
    class UnidadTiempo(models.TextChoices):
        MINUTOS = 'min', 'Minutos'
        HORAS = 'hrs', 'Horas'
        SEGUNDOS = 'seg', 'Segundos'

    bom = models.ForeignKey(
        BOM,
        on_delete=models.CASCADE,
        related_name='operaciones',
    )
    secuencia = models.PositiveSmallIntegerField('Secuencia', default=1)
    nombre = models.CharField('Nombre de operación', max_length=120)
    descripcion = models.TextField('Descripción', blank=True)
    linea_produccion = models.CharField('Línea de producción', max_length=120, blank=True)
    tiempo_estimado = models.DecimalField(
        'Tiempo estimado', max_digits=8, decimal_places=2, null=True, blank=True
    )
    unidad_tiempo = models.CharField(
        'Unidad de tiempo',
        max_length=4,
        choices=UnidadTiempo.choices,
        default=UnidadTiempo.MINUTOS,
    )
    recurso_maquina = models.CharField('Máquina / Equipo', max_length=120, blank=True)
    operadores_requeridos = models.PositiveSmallIntegerField('Operadores requeridos', default=1)

    def __str__(self):
        return f"{self.bom.codigo} | Op{self.secuencia}: {self.nombre}"

    class Meta:
        verbose_name = 'Operación BOM'
        verbose_name_plural = 'Operaciones BOM'
        ordering = ['bom', 'secuencia']
        constraints = [
            models.UniqueConstraint(
                fields=['bom', 'secuencia'],
                name='unique_bom_secuencia',
            )
        ]


class Proveedor(models.Model):
    nombre = models.CharField('Nombre', max_length=200, unique=True)
    descripcion = models.TextField('Descripción', blank=True)
    telefono = models.CharField('Teléfono', max_length=30, blank=True)
    email = models.EmailField('Correo electrónico', blank=True)
    materiales = models.ManyToManyField(
        Material,
        blank=True,
        verbose_name='Materiales',
        help_text='Materiales que suministra este proveedor'
    )
    activo = models.BooleanField('Activo', default=True)
    fecha_creacion = models.DateTimeField(auto_now_add=True, null=True, blank=True)

    def __str__(self):
        return self.nombre

    class Meta:
        verbose_name = 'Proveedor'
        verbose_name_plural = 'Proveedores'
        ordering = ['nombre']


class ProveedorMaterialPrecio(models.Model):
    proveedor = models.ForeignKey(
        Proveedor,
        on_delete=models.CASCADE,
        related_name='precios_materiales',
    )
    material = models.ForeignKey(
        Material,
        on_delete=models.CASCADE,
        related_name='precios_por_proveedor',
    )
    precio_unitario = models.DecimalField('Precio unitario', max_digits=14, decimal_places=2, default=0)
    fecha_actualizacion = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.proveedor.nombre} - {self.material.sku}: {self.precio_unitario}"

    class Meta:
        verbose_name = 'Precio material por proveedor'
        verbose_name_plural = 'Precios de materiales por proveedor'
        constraints = [
            models.UniqueConstraint(
                fields=['proveedor', 'material'],
                name='unique_proveedor_material_precio',
            )
        ]


class ClienteCompra(models.Model):
    codigo = models.CharField('Código', max_length=20, unique=True)
    nombre = models.CharField('Nombre', max_length=200, unique=True)
    contacto = models.CharField('Contacto principal', max_length=150, blank=True)
    email = models.EmailField('Correo electrónico', blank=True)
    telefono = models.CharField('Teléfono', max_length=30, blank=True)
    activo = models.BooleanField('Activo', default=True)
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_actualizacion = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.nombre

    class Meta:
        verbose_name = 'Cliente de compra'
        verbose_name_plural = 'Clientes de compra'
        ordering = ['nombre']


class Almacen(models.Model):
    codigo = models.CharField('Codigo', max_length=20, unique=True)
    nombre = models.CharField('Nombre', max_length=120, unique=True)
    descripcion = models.CharField('Descripcion', max_length=255, blank=True)
    activo = models.BooleanField('Activo', default=True)

    def __str__(self):
        return f"{self.codigo} - {self.nombre}"

    class Meta:
        verbose_name = 'Almacen'
        verbose_name_plural = 'Almacenes'
        ordering = ['codigo']


class InventarioAlmacen(models.Model):
    material = models.ForeignKey(
        Material,
        on_delete=models.CASCADE,
        related_name='inventarios_almacen',
    )
    almacen = models.ForeignKey(
        Almacen,
        on_delete=models.CASCADE,
        related_name='inventarios_material',
    )
    lote = models.CharField('Lote', max_length=80, blank=True)
    stock_actual = models.DecimalField('Stock actual', max_digits=14, decimal_places=2, default=0)
    fecha_actualizacion = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.material.sku} @ {self.almacen.codigo}"

    class Meta:
        verbose_name = 'Inventario por almacen'
        verbose_name_plural = 'Inventario por almacen'
        ordering = ['almacen__codigo', 'material__sku']
        constraints = [
            models.UniqueConstraint(
                fields=['material', 'almacen', 'lote'],
                name='unique_material_almacen_lote',
            )
        ]


class RecepcionMaterial(models.Model):
    class EstadoRecepcion(models.TextChoices):
        BORRADOR = 'BORRADOR', 'Borrador'
        ENVIADA = 'ENVIADA', 'Enviada'

    class AccionRecomendada(models.TextChoices):
        ACEPTAR_TODO = 'ACEPTAR_TODO', 'Aceptar todo el embarque'
        ACEPTAR_PARCIAL = 'ACEPTAR_PARCIAL', 'Aceptar parcial y levantar incidencia'
        RECHAZAR = 'RECHAZAR', 'Rechazar embarque'
        CUARENTENA = 'CUARENTENA', 'Cuarentena para revisión de calidad'

    fecha_recepcion = models.DateField('Fecha de recepción')
    hora_recepcion = models.TimeField('Hora de recepción')
    proveedor = models.CharField('Proveedor', max_length=200)
    proveedor_registrado = models.ForeignKey(
        Proveedor,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='recepciones_material',
    )
    orden_compra = models.CharField('Orden de compra', max_length=80, blank=True)
    factura = models.CharField('Factura / Remisión', max_length=80, blank=True)
    transportista = models.CharField('Transportista', max_length=200, blank=True)
    placas = models.CharField('Placas de unidad', max_length=30, blank=True)

    chk_oc = models.BooleanField(default=False)
    chk_cantidad = models.BooleanField(default=False)
    chk_empaque = models.BooleanField(default=False)
    chk_lote = models.BooleanField(default=False)
    chk_vigencia = models.BooleanField(default=False)
    chk_certificado = models.BooleanField(default=False)
    chk_estado_fisico = models.BooleanField(default=False)
    chk_foto = models.BooleanField(default=False)
    chk_calidad = models.BooleanField(default=False)

    observaciones = models.TextField('Observaciones', blank=True)
    accion_recomendada = models.CharField(
        'Acción recomendada',
        max_length=30,
        choices=AccionRecomendada.choices,
        blank=True,
    )
    estado = models.CharField(
        'Estado',
        max_length=10,
        choices=EstadoRecepcion.choices,
        default=EstadoRecepcion.ENVIADA,
    )

    creado_por = models.ForeignKey(
        UsuarioERP,
        on_delete=models.PROTECT,
        related_name='recepciones_material',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Recepción {self.id} - {self.proveedor} - {self.fecha_recepcion}"

    class Meta:
        verbose_name = 'Recepción de material'
        verbose_name_plural = 'Recepciones de material'
        ordering = ['-fecha_creacion']


class RecepcionMaterialDetalle(models.Model):
    class EstatusDetalle(models.TextChoices):
        ACEPTADO = 'ACEPTADO', 'Aceptado'
        DIFERENCIA = 'DIFERENCIA', 'Diferencia'
        RECHAZADO = 'RECHAZADO', 'Rechazado'

    recepcion = models.ForeignKey(
        RecepcionMaterial,
        on_delete=models.CASCADE,
        related_name='detalles',
    )
    material = models.ForeignKey(
        Material,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='recepciones_detalle',
    )
    sku = models.CharField(max_length=50)
    descripcion = models.CharField(max_length=255)
    um = models.CharField('Unidad de medida', max_length=20, blank=True)
    cantidad_oc = models.DecimalField('Cantidad OC', max_digits=12, decimal_places=2, default=0)
    cantidad_recibida = models.DecimalField('Cantidad recibida', max_digits=12, decimal_places=2, default=0)
    lote = models.CharField(max_length=80, blank=True)
    ubicacion_destino = models.CharField(max_length=80, blank=True)
    estatus = models.CharField(max_length=12, choices=EstatusDetalle.choices, default=EstatusDetalle.ACEPTADO)

    def __str__(self):
        return f"{self.sku} - {self.descripcion} ({self.recepcion_id})"

    class Meta:
        verbose_name = 'Detalle de recepción'
        verbose_name_plural = 'Detalles de recepción'


class SalidaLinea(models.Model):
    fecha_salida = models.DateField('Fecha de salida')
    hora_salida = models.TimeField('Hora de salida')
    linea_destino = models.CharField('Linea destino', max_length=120)
    orden_produccion = models.CharField('Orden de produccion', max_length=80, blank=True)
    turno = models.CharField('Turno', max_length=40, blank=True)
    observaciones = models.TextField('Observaciones', blank=True)
    creado_por = models.ForeignKey(
        UsuarioERP,
        on_delete=models.PROTECT,
        related_name='salidas_linea',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Salida {self.id} - {self.linea_destino} - {self.fecha_salida}"

    class Meta:
        verbose_name = 'Salida a linea'
        verbose_name_plural = 'Salidas a linea'
        ordering = ['-fecha_creacion']


class SalidaLineaDetalle(models.Model):
    salida = models.ForeignKey(
        SalidaLinea,
        on_delete=models.CASCADE,
        related_name='detalles',
    )
    almacen_origen = models.ForeignKey(
        Almacen,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='salidas_detalle',
    )
    material = models.ForeignKey(
        Material,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='salidas_detalle',
    )
    sku = models.CharField(max_length=50)
    descripcion = models.CharField(max_length=255)
    um = models.CharField('Unidad de medida', max_length=20, blank=True)
    cantidad_enviada = models.DecimalField('Cantidad enviada', max_digits=12, decimal_places=2, default=0)
    lote = models.CharField(max_length=80, blank=True)

    def __str__(self):
        return f"{self.sku} -> {self.salida.linea_destino} ({self.cantidad_enviada})"

    class Meta:
        verbose_name = 'Detalle salida a linea'
        verbose_name_plural = 'Detalles salida a linea'


class TransferenciaAlmacen(models.Model):
    fecha_transferencia = models.DateField('Fecha de transferencia')
    hora_transferencia = models.TimeField('Hora de transferencia')
    almacen_origen = models.ForeignKey(
        Almacen,
        on_delete=models.PROTECT,
        related_name='transferencias_origen',
    )
    almacen_destino = models.ForeignKey(
        Almacen,
        on_delete=models.PROTECT,
        related_name='transferencias_destino',
    )
    referencia = models.CharField('Referencia', max_length=80, blank=True)
    motivo = models.TextField('Motivo', blank=True)
    creado_por = models.ForeignKey(
        UsuarioERP,
        on_delete=models.PROTECT,
        related_name='transferencias_almacen',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Transferencia {self.id} - {self.almacen_origen.codigo} a {self.almacen_destino.codigo}"

    class Meta:
        verbose_name = 'Transferencia entre almacenes'
        verbose_name_plural = 'Transferencias entre almacenes'
        ordering = ['-fecha_creacion']


class TransferenciaAlmacenDetalle(models.Model):
    transferencia = models.ForeignKey(
        TransferenciaAlmacen,
        on_delete=models.CASCADE,
        related_name='detalles',
    )
    material = models.ForeignKey(
        Material,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='transferencias_detalle',
    )
    sku = models.CharField(max_length=50)
    descripcion = models.CharField(max_length=255)
    um = models.CharField('Unidad de medida', max_length=20, blank=True)
    cantidad_transferida = models.DecimalField('Cantidad transferida', max_digits=12, decimal_places=2, default=0)
    lote = models.CharField(max_length=80, blank=True)

    def __str__(self):
        return f"{self.sku} {self.lote or '-'} ({self.cantidad_transferida})"

    class Meta:
        verbose_name = 'Detalle transferencia entre almacenes'
        verbose_name_plural = 'Detalles transferencia entre almacenes'


class OrdenCompra(models.Model):
    class EstadoOrden(models.TextChoices):
        BORRADOR = 'BORRADOR', 'Borrador'
        APROBADA = 'APROBADA', 'Aprobada'
        ENVIADA = 'ENVIADA', 'Enviada'
        PARCIAL = 'PARCIAL', 'Parcial'
        RECIBIDA = 'RECIBIDA', 'Recibida'
        CANCELADA = 'CANCELADA', 'Cancelada'

    folio = models.CharField('Folio', max_length=30, unique=True)
    proveedor = models.ForeignKey(
        Proveedor,
        on_delete=models.PROTECT,
        related_name='ordenes_compra',
    )
    requerimiento_origen = models.ForeignKey(
        'RequerimientoMaterialProduccion',
        on_delete=models.SET_NULL,
        related_name='ordenes_compra_generadas',
        null=True,
        blank=True,
    )
    fecha_orden = models.DateField('Fecha de orden')
    fecha_prometida = models.DateField('Fecha prometida', null=True, blank=True)
    tiempo_entrega_estimado_dias = models.PositiveIntegerField('Tiempo estimado entrega (días)', default=0)
    condiciones_pago = models.CharField('Condiciones de pago', max_length=120, blank=True)
    observaciones = models.TextField('Observaciones', blank=True)
    estado = models.CharField(
        'Estado',
        max_length=12,
        choices=EstadoOrden.choices,
        default=EstadoOrden.BORRADOR,
    )
    creada_desde_mfg = models.BooleanField('Creada desde requerimiento MFG', default=False)
    total_estimado = models.DecimalField('Total estimado', max_digits=14, decimal_places=2, default=0)
    creado_por = models.ForeignKey(
        UsuarioERP,
        on_delete=models.PROTECT,
        related_name='ordenes_compra',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.folio} - {self.proveedor.nombre}"

    class Meta:
        verbose_name = 'Orden de compra'
        verbose_name_plural = 'Órdenes de compra'
        ordering = ['-fecha_creacion']


class OrdenCompraDetalle(models.Model):
    orden = models.ForeignKey(
        OrdenCompra,
        on_delete=models.CASCADE,
        related_name='detalles',
    )
    material = models.ForeignKey(
        Material,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ordenes_compra_detalle',
    )
    sku = models.CharField(max_length=50)
    descripcion = models.CharField(max_length=255)
    um = models.CharField('Unidad de medida', max_length=20, blank=True)
    cantidad_pedida = models.DecimalField('Cantidad pedida', max_digits=12, decimal_places=2, default=0)
    precio_unitario = models.DecimalField('Precio unitario', max_digits=14, decimal_places=2, default=0)
    subtotal = models.DecimalField('Subtotal', max_digits=14, decimal_places=2, default=0)

    def __str__(self):
        return f"{self.orden.folio} - {self.sku}"

    class Meta:
        verbose_name = 'Detalle orden de compra'
        verbose_name_plural = 'Detalles orden de compra'


class PlanProduccion(models.Model):
    class EstadoPlan(models.TextChoices):
        BORRADOR = 'BORRADOR', 'Borrador'
        APROBADO = 'APROBADO', 'Aprobado'
        EN_PROCESO = 'EN_PROCESO', 'En proceso'
        COMPLETADO = 'COMPLETADO', 'Completado'
        CANCELADO = 'CANCELADO', 'Cancelado'

    folio = models.CharField('Folio', max_length=30, unique=True)
    bom = models.ForeignKey(
        BOM,
        on_delete=models.PROTECT,
        related_name='planes_produccion',
        verbose_name='BOM / Producto',
    )
    cantidad_planificada = models.DecimalField(
        'Cantidad a producir', max_digits=12, decimal_places=2
    )
    fecha_inicio = models.DateField('Fecha inicio')
    fecha_fin = models.DateField('Fecha fin estimada')
    linea_produccion = models.CharField('Línea de producción', max_length=120, blank=True)
    turno = models.CharField('Turno', max_length=40, blank=True)
    observaciones = models.TextField('Observaciones', blank=True)
    estado = models.CharField(
        'Estado',
        max_length=12,
        choices=EstadoPlan.choices,
        default=EstadoPlan.BORRADOR,
    )
    creado_por = models.ForeignKey(
        UsuarioERP,
        on_delete=models.PROTECT,
        related_name='planes_produccion',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_actualizacion = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.folio} - {self.bom.producto}"

    class Meta:
        verbose_name = 'Plan de producción'
        verbose_name_plural = 'Planes de producción'
        ordering = ['-fecha_creacion']


class PlanProduccionDetalle(models.Model):
    class EstadoMaterial(models.TextChoices):
        DISPONIBLE = 'DISPONIBLE', 'Disponible en inventario'
        PARCIAL = 'PARCIAL', 'Stock parcial'
        REQUIERE_COMPRA = 'REQUIERE_COMPRA', 'Requiere orden de compra'

    plan = models.ForeignKey(
        PlanProduccion,
        on_delete=models.CASCADE,
        related_name='detalles',
    )
    material = models.ForeignKey(
        Material,
        on_delete=models.PROTECT,
        related_name='plan_produccion_detalles',
    )
    cantidad_requerida = models.DecimalField(
        'Cantidad requerida', max_digits=12, decimal_places=3
    )
    cantidad_disponible = models.DecimalField(
        'Cantidad disponible en inventario', max_digits=12, decimal_places=3, default=0
    )
    cantidad_faltante = models.DecimalField(
        'Cantidad faltante (a comprar)', max_digits=12, decimal_places=3, default=0
    )
    estado_material = models.CharField(
        'Estado de material',
        max_length=20,
        choices=EstadoMaterial.choices,
        default=EstadoMaterial.DISPONIBLE,
    )

    def __str__(self):
        return f"{self.plan.folio} - {self.material.sku}"

    class Meta:
        verbose_name = 'Detalle plan de producción'
        verbose_name_plural = 'Detalles plan de producción'
        ordering = ['material__sku']


class RequerimientoMaterialProduccion(models.Model):
    class EstadoRequerimiento(models.TextChoices):
        BORRADOR = 'BORRADOR', 'Borrador'
        ENVIADO_FINANZAS = 'ENVIADO_FINANZAS', 'Enviado a finanzas'

    folio = models.CharField('Folio', max_length=30, unique=True)
    bom = models.ForeignKey(
        BOM,
        on_delete=models.PROTECT,
        related_name='requerimientos_materiales',
        verbose_name='BOM / Producto',
    )
    cantidad_planificada = models.DecimalField('Cantidad a producir', max_digits=12, decimal_places=2)
    notas = models.TextField('Notas', blank=True)
    estado = models.CharField(
        'Estado',
        max_length=18,
        choices=EstadoRequerimiento.choices,
        default=EstadoRequerimiento.BORRADOR,
    )
    creado_por = models.ForeignKey(
        UsuarioERP,
        on_delete=models.PROTECT,
        related_name='requerimientos_materiales_produccion',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_envio_finanzas = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.folio} - {self.bom.producto}"

    class Meta:
        verbose_name = 'Requerimiento de material de producción'
        verbose_name_plural = 'Requerimientos de material de producción'
        ordering = ['-fecha_creacion']


class RequerimientoMaterialProduccionDetalle(models.Model):
    requerimiento = models.ForeignKey(
        RequerimientoMaterialProduccion,
        on_delete=models.CASCADE,
        related_name='detalles',
    )
    material = models.ForeignKey(
        Material,
        on_delete=models.PROTECT,
        related_name='requerimientos_produccion_detalle',
    )
    cantidad_base_requerida = models.DecimalField('Cantidad base requerida', max_digits=12, decimal_places=3)
    cantidad_con_scrap = models.DecimalField('Cantidad con scrap', max_digits=12, decimal_places=3)
    stock_actual = models.DecimalField('Stock actual', max_digits=12, decimal_places=3, default=0)
    cantidad_sugerida_compra = models.DecimalField('Cantidad sugerida compra', max_digits=12, decimal_places=3, default=0)
    cantidad_solicitada = models.DecimalField('Cantidad solicitada', max_digits=12, decimal_places=3, default=0)
    observaciones = models.CharField('Observaciones', max_length=255, blank=True)

    def __str__(self):
        return f"{self.requerimiento.folio} - {self.material.sku}"

    class Meta:
        verbose_name = 'Detalle requerimiento material producción'
        verbose_name_plural = 'Detalles requerimiento material producción'
        ordering = ['material__sku']
        constraints = [
            models.UniqueConstraint(
                fields=['requerimiento', 'material'],
                name='unique_req_produccion_material',
            )
        ]


class OrdenFabricacion(models.Model):
    class EstadoOF(models.TextChoices):
        BORRADOR = 'BORRADOR', 'Borrador'
        EN_PROCESO = 'EN_PROCESO', 'En proceso'
        PAUSADA = 'PAUSADA', 'Pausada'
        COMPLETADA = 'COMPLETADA', 'Completada'
        CANCELADA = 'CANCELADA', 'Cancelada'

    folio = models.CharField('Folio', max_length=30, unique=True)
    plan = models.ForeignKey(
        PlanProduccion,
        on_delete=models.PROTECT,
        related_name='ordenes_fabricacion',
        null=True,
        blank=True,
        verbose_name='Plan de producción',
    )
    bom = models.ForeignKey(
        BOM,
        on_delete=models.PROTECT,
        related_name='ordenes_fabricacion',
        verbose_name='BOM / Producto',
    )
    cantidad_planificada = models.DecimalField('Cantidad planificada', max_digits=12, decimal_places=2)
    cantidad_producida = models.DecimalField(
        'Cantidad producida real', max_digits=12, decimal_places=2, default=0
    )
    linea_produccion = models.CharField('Línea de producción', max_length=120, blank=True)
    turno = models.CharField('Turno', max_length=60, blank=True)
    estado = models.CharField(
        'Estado',
        max_length=12,
        choices=EstadoOF.choices,
        default=EstadoOF.BORRADOR,
    )
    fecha_inicio_programada = models.DateField('Fecha inicio programada', null=True, blank=True)
    fecha_fin_programada = models.DateField('Fecha fin programada', null=True, blank=True)
    fecha_inicio_real = models.DateTimeField('Inicio real', null=True, blank=True)
    fecha_fin_real = models.DateTimeField('Fin real', null=True, blank=True)
    observaciones = models.TextField('Observaciones', blank=True)
    creado_por = models.ForeignKey(
        UsuarioERP,
        on_delete=models.PROTECT,
        related_name='ordenes_fabricacion',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_actualizacion = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.folio} - {self.bom.producto}"

    class Meta:
        verbose_name = 'Orden de fabricación'
        verbose_name_plural = 'Órdenes de fabricación'
        ordering = ['-fecha_creacion']


class OrdenFabricacionDetalle(models.Model):
    orden = models.ForeignKey(
        OrdenFabricacion,
        on_delete=models.CASCADE,
        related_name='detalles',
    )
    material = models.ForeignKey(
        Material,
        on_delete=models.PROTECT,
        related_name='ordenes_fabricacion_detalle',
    )
    cantidad_requerida = models.DecimalField('Cantidad requerida', max_digits=12, decimal_places=3)
    cantidad_consumida = models.DecimalField(
        'Cantidad consumida real', max_digits=12, decimal_places=3, default=0
    )
    observaciones = models.CharField('Observaciones', max_length=255, blank=True)

    def __str__(self):
        return f"{self.orden.folio} - {self.material.sku}"

    class Meta:
        verbose_name = 'Detalle orden de fabricación'
        verbose_name_plural = 'Detalles orden de fabricación'
        ordering = ['material__sku']
        constraints = [
            models.UniqueConstraint(
                fields=['orden', 'material'],
                name='unique_of_material',
            )
        ]


class LoteProduccion(models.Model):
    class EstadoLote(models.TextChoices):
        CAPTURADO = 'CAPTURADO', 'Capturado'
        VALIDADO = 'VALIDADO', 'Validado'
        RECHAZADO = 'RECHAZADO', 'Rechazado'

    folio = models.CharField('Folio', max_length=30, unique=True)
    bom = models.ForeignKey(
        BOM,
        on_delete=models.PROTECT,
        related_name='lotes_produccion',
        verbose_name='Producto (BOM)',
    )
    orden_fabricacion = models.ForeignKey(
        OrdenFabricacion,
        on_delete=models.PROTECT,
        related_name='lotes_produccion',
        null=True,
        blank=True,
        verbose_name='Orden de fabricación',
    )
    cliente_destino = models.ForeignKey(
        ClienteCompra,
        on_delete=models.PROTECT,
        related_name='lotes_produccion',
        null=True,
        blank=True,
        verbose_name='Cliente destino',
    )
    fecha_captura = models.DateField('Fecha de captura')
    hora_captura = models.TimeField('Hora de captura')
    linea_produccion = models.CharField('Línea de producción', max_length=120, blank=True)
    turno = models.CharField('Turno', max_length=60, blank=True)
    cantidad_producida = models.DecimalField('Cantidad producida', max_digits=12, decimal_places=2)
    operador = models.CharField('Operador / Responsable', max_length=200, blank=True)
    estado = models.CharField(
        'Estado',
        max_length=12,
        choices=EstadoLote.choices,
        default=EstadoLote.CAPTURADO,
    )
    observaciones = models.TextField('Observaciones', blank=True)
    creado_por = models.ForeignKey(
        UsuarioERP,
        on_delete=models.PROTECT,
        related_name='lotes_produccion',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_actualizacion = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.folio} - {self.bom.producto} ({self.cantidad_producida})"

    class Meta:
        verbose_name = 'Lote de producción'
        verbose_name_plural = 'Lotes de producción'
        ordering = ['-fecha_creacion']


class ReclamoCliente(models.Model):
    class TipoReclamo(models.TextChoices):
        DEFECTO_VISUAL = 'defecto_visual', 'Defecto visual'
        FUNCIONAL = 'funcional', 'Falla funcional'
        DOCUMENTAL = 'documental', 'Error documental'
        LOGISTICO = 'logistico', 'Incidencia logística'

    class EstadoReclamo(models.TextChoices):
        ABIERTO = 'abierto', 'Abierto'
        EN_ANALISIS = 'en_analisis', 'En análisis'
        EN_CONTENCION = 'en_contencion', 'En contención'
        CERRADO = 'cerrado', 'Cerrado'

    class PrioridadReclamo(models.TextChoices):
        ALTA = 'alta', 'Alta'
        MEDIA = 'media', 'Media'
        BAJA = 'baja', 'Baja'

    folio = models.CharField('Folio de reclamo', max_length=40, unique=True)
    cliente = models.CharField('Cliente', max_length=200)
    cliente_compra = models.ForeignKey(
        ClienteCompra,
        on_delete=models.PROTECT,
        related_name='reclamos_cliente',
        null=True,
        blank=True,
        verbose_name='Cliente catálogo',
    )
    producto_lote = models.CharField('Producto / lote', max_length=200, blank=True)
    tipo_reclamo = models.CharField('Tipo de reclamo', max_length=30, choices=TipoReclamo.choices)
    estado_reclamo = models.CharField(
        'Estado del reclamo',
        max_length=20,
        choices=EstadoReclamo.choices,
        default=EstadoReclamo.ABIERTO,
    )
    prioridad = models.CharField(
        'Prioridad',
        max_length=10,
        choices=PrioridadReclamo.choices,
        default=PrioridadReclamo.MEDIA,
    )
    descripcion = models.TextField('Descripción', blank=True)
    creado_por = models.ForeignKey(
        UsuarioERP,
        on_delete=models.PROTECT,
        related_name='reclamos_cliente',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_actualizacion = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.folio} - {self.cliente}"

    class Meta:
        verbose_name = 'Reclamo de cliente'
        verbose_name_plural = 'Reclamos de cliente'
        ordering = ['-fecha_actualizacion', '-fecha_creacion']


class ReporteKPIProduccion(models.Model):
    fecha_inicio = models.DateField('Fecha inicio reporte')
    fecha_fin = models.DateField('Fecha fin reporte')
    disponibilidad = models.DecimalField('Disponibilidad', max_digits=7, decimal_places=2, default=0)
    rendimiento = models.DecimalField('Rendimiento', max_digits=7, decimal_places=2, default=0)
    calidad = models.DecimalField('Calidad', max_digits=7, decimal_places=2, default=0)
    oee = models.DecimalField('OEE', max_digits=7, decimal_places=2, default=0)
    tiempo_ciclo_promedio = models.DecimalField('Tiempo de ciclo promedio', max_digits=10, decimal_places=2, default=0)
    tasa_rechazo = models.DecimalField('Tasa de rechazo', max_digits=7, decimal_places=2, default=0)
    cumplimiento_ordenes = models.DecimalField('Cumplimiento de órdenes', max_digits=7, decimal_places=2, default=0)
    costo_planificado = models.DecimalField('Costo planificado', max_digits=14, decimal_places=2, default=0)
    costo_real = models.DecimalField('Costo real', max_digits=14, decimal_places=2, default=0)
    variacion_costos = models.DecimalField('Variación de costos', max_digits=14, decimal_places=2, default=0)
    utilizacion_maquinas = models.DecimalField('Utilización de máquinas', max_digits=7, decimal_places=2, default=0)
    utilizacion_personal = models.DecimalField('Utilización de personal', max_digits=7, decimal_places=2, default=0)
    utilizacion_recursos = models.DecimalField('Utilización de recursos', max_digits=7, decimal_places=2, default=0)
    alertas = models.JSONField('Alertas', default=list, blank=True)
    detalle = models.JSONField('Detalle', default=dict, blank=True)
    generado_por = models.ForeignKey(
        UsuarioERP,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reportes_kpi_produccion',
    )
    fecha_generacion = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"KPIs Producción {self.fecha_inicio} - {self.fecha_fin}"

    class Meta:
        verbose_name = 'Reporte KPI de producción'
        verbose_name_plural = 'Reportes KPI de producción'
        ordering = ['-fecha_generacion']


class CostoHoraMaquina(models.Model):
    linea_produccion = models.CharField('Línea de producción', max_length=120)
    maquina_nombre = models.CharField('Máquina / Equipo', max_length=150)
    costo_hora = models.DecimalField('Costo hora real', max_digits=12, decimal_places=2)
    activo = models.BooleanField('Activo', default=True)
    notas = models.CharField('Notas', max_length=255, blank=True)
    registrado_por = models.ForeignKey(
        UsuarioERP,
        on_delete=models.PROTECT,
        related_name='costos_maquina_registrados',
    )
    actualizado_por = models.ForeignKey(
        UsuarioERP,
        on_delete=models.PROTECT,
        related_name='costos_maquina_actualizados',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_actualizacion = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.linea_produccion} - {self.maquina_nombre}"

    class Meta:
        verbose_name = 'Costo hora máquina'
        verbose_name_plural = 'Costos hora de máquina'
        ordering = ['linea_produccion', 'maquina_nombre']
        constraints = [
            models.UniqueConstraint(
                fields=['linea_produccion', 'maquina_nombre'],
                name='unique_linea_maquina_costo',
            )
        ]


class CostoHoraOperador(models.Model):
    operador = models.ForeignKey(
        UsuarioERP,
        on_delete=models.PROTECT,
        related_name='costos_hora_operador',
    )
    nomina_hora = models.DecimalField('Nómina por hora', max_digits=12, decimal_places=2)
    porcentaje_asistencia = models.DecimalField('Asistencia %', max_digits=5, decimal_places=2, default=100)
    factor_desempeno = models.DecimalField('Desempeño %', max_digits=5, decimal_places=2, default=100)
    costo_hora_real = models.DecimalField('Costo hora real', max_digits=12, decimal_places=2, default=0)
    activo = models.BooleanField('Activo', default=True)
    notas = models.CharField('Notas', max_length=255, blank=True)
    registrado_por = models.ForeignKey(
        UsuarioERP,
        on_delete=models.PROTECT,
        related_name='costos_operador_registrados',
    )
    actualizado_por = models.ForeignKey(
        UsuarioERP,
        on_delete=models.PROTECT,
        related_name='costos_operador_actualizados',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_actualizacion = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        asistencia = self.porcentaje_asistencia or 0
        desempeno = self.factor_desempeno or 0
        self.costo_hora_real = (self.nomina_hora or 0) * (asistencia / 100) * (desempeno / 100)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.operador}"

    class Meta:
        verbose_name = 'Costo hora operador'
        verbose_name_plural = 'Costos hora de operador'
        ordering = ['operador__username']
        constraints = [
            models.UniqueConstraint(
                fields=['operador'],
                name='unique_operador_costo_hora',
            )
        ]


class RegistroUsoRecursoProduccion(models.Model):
    class TipoRecurso(models.TextChoices):
        MAQUINA = 'MAQUINA', 'Máquina'
        OPERADOR = 'OPERADOR', 'Operador'

    orden = models.ForeignKey(
        OrdenFabricacion,
        on_delete=models.CASCADE,
        related_name='usos_recursos',
    )
    tipo_recurso = models.CharField('Tipo recurso', max_length=12, choices=TipoRecurso.choices)
    costo_maquina = models.ForeignKey(
        CostoHoraMaquina,
        on_delete=models.PROTECT,
        related_name='usos_orden',
        null=True,
        blank=True,
    )
    costo_operador = models.ForeignKey(
        CostoHoraOperador,
        on_delete=models.PROTECT,
        related_name='usos_orden',
        null=True,
        blank=True,
    )
    horas_reales = models.DecimalField('Horas reales', max_digits=10, decimal_places=2)
    costo_total = models.DecimalField('Costo total', max_digits=14, decimal_places=2, default=0)
    notas = models.CharField('Notas', max_length=255, blank=True)
    registrado_por = models.ForeignKey(
        UsuarioERP,
        on_delete=models.PROTECT,
        related_name='usos_recurso_registrados',
    )
    actualizado_por = models.ForeignKey(
        UsuarioERP,
        on_delete=models.PROTECT,
        related_name='usos_recurso_actualizados',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_actualizacion = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        tarifa = 0
        if self.tipo_recurso == self.TipoRecurso.MAQUINA and self.costo_maquina_id:
            tarifa = self.costo_maquina.costo_hora
        elif self.tipo_recurso == self.TipoRecurso.OPERADOR and self.costo_operador_id:
            tarifa = self.costo_operador.costo_hora_real
        self.costo_total = (self.horas_reales or 0) * tarifa
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.orden.folio} - {self.get_tipo_recurso_display()}"

    class Meta:
        verbose_name = 'Uso de recurso de producción'
        verbose_name_plural = 'Usos de recursos de producción'
        ordering = ['-fecha_creacion']


class RegistroScrapDefecto(models.Model):
    class TipoDefecto(models.TextChoices):
        SCRAP = 'SCRAP', 'Scrap'
        VISUAL = 'VISUAL', 'Defecto visual'
        FUNCIONAL = 'FUNCIONAL', 'Defecto funcional'
        DIMENSIONAL = 'DIMENSIONAL', 'Defecto dimensional'
        PROCESO = 'PROCESO', 'Defecto de proceso'

    orden = models.ForeignKey(
        OrdenFabricacion,
        on_delete=models.CASCADE,
        related_name='scraps_defectos',
        null=True,
        blank=True,
    )
    lote = models.ForeignKey(
        LoteProduccion,
        on_delete=models.CASCADE,
        related_name='scraps_defectos',
        null=True,
        blank=True,
    )
    cantidad_defectos = models.DecimalField('Cantidad defectos', max_digits=12, decimal_places=2)
    tipo_defecto = models.CharField('Tipo de defecto', max_length=20, choices=TipoDefecto.choices)
    causa = models.CharField('Causa', max_length=150)
    descripcion = models.TextField('Descripción', blank=True)
    registrado_por = models.ForeignKey(
        UsuarioERP,
        on_delete=models.PROTECT,
        related_name='scraps_registrados',
    )
    actualizado_por = models.ForeignKey(
        UsuarioERP,
        on_delete=models.PROTECT,
        related_name='scraps_actualizados',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_actualizacion = models.DateTimeField(auto_now=True)

    def __str__(self):
        referencia = self.orden.folio if self.orden_id else (self.lote.folio if self.lote_id else 'Sin referencia')
        return f"{referencia} - {self.tipo_defecto}"

    class Meta:
        verbose_name = 'Registro scrap / defecto'
        verbose_name_plural = 'Registros scrap / defectos'
        ordering = ['-fecha_creacion']


class InformeValidacionDefectoQA(models.Model):
    class ResultadoValidacion(models.TextChoices):
        EN_ANALISIS = 'EN_ANALISIS', 'En análisis'
        VALIDADO = 'VALIDADO', 'Validado'
        RECHAZADO = 'RECHAZADO', 'Rechazado'

    defecto = models.OneToOneField(
        RegistroScrapDefecto,
        on_delete=models.CASCADE,
        related_name='informe_qa',
    )
    resultado_validacion = models.CharField(
        'Resultado validación',
        max_length=15,
        choices=ResultadoValidacion.choices,
        default=ResultadoValidacion.EN_ANALISIS,
    )
    falla_maquina = models.BooleanField('Falla de máquina', default=False)
    informe = models.TextField('Informe QA')
    acciones_contencion = models.TextField('Acciones de contención', blank=True)
    validado_por = models.ForeignKey(
        UsuarioERP,
        on_delete=models.PROTECT,
        related_name='informes_defectos_qa',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_actualizacion = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"QA {self.defecto_id} - {self.get_resultado_validacion_display()}"

    class Meta:
        verbose_name = 'Informe validación defecto QA'
        verbose_name_plural = 'Informes de validación de defectos QA'
        ordering = ['-fecha_actualizacion']


class CuentaContable(RegistroAuditable):
    class TipoCuenta(models.TextChoices):
        ACTIVO = 'ACTIVO', 'Activo'
        PASIVO = 'PASIVO', 'Pasivo'
        CAPITAL = 'CAPITAL', 'Capital'
        INGRESO = 'INGRESO', 'Ingreso'
        GASTO = 'GASTO', 'Gasto'

    codigo = models.CharField('Código', max_length=20, unique=True)
    nombre = models.CharField('Nombre', max_length=150)
    tipo = models.CharField('Tipo', max_length=10, choices=TipoCuenta.choices)
    descripcion = models.TextField('Descripción', blank=True)
    activa = models.BooleanField('Activa', default=True)

    def __str__(self):
        return f"{self.codigo} - {self.nombre}"

    class Meta:
        verbose_name = 'Cuenta contable'
        verbose_name_plural = 'Cuentas contables'
        ordering = ['codigo']


class PolizaContable(RegistroAuditable):
    class TipoPoliza(models.TextChoices):
        DIARIO = 'DIARIO', 'Diario'
        INGRESO = 'INGRESO', 'Ingreso'
        EGRESO = 'EGRESO', 'Egreso'
        AJUSTE = 'AJUSTE', 'Ajuste'

    class EstadoPoliza(models.TextChoices):
        BORRADOR = 'BORRADOR', 'Borrador'
        CONTABILIZADA = 'CONTABILIZADA', 'Contabilizada'
        CANCELADA = 'CANCELADA', 'Cancelada'

    folio = models.CharField('Folio', max_length=30, unique=True)
    fecha_poliza = models.DateField('Fecha póliza')
    tipo = models.CharField('Tipo', max_length=10, choices=TipoPoliza.choices, default=TipoPoliza.DIARIO)
    concepto = models.CharField('Concepto', max_length=255)
    referencia = models.CharField('Referencia', max_length=120, blank=True)
    estado = models.CharField('Estado', max_length=15, choices=EstadoPoliza.choices, default=EstadoPoliza.BORRADOR)

    def __str__(self):
        return self.folio

    class Meta:
        verbose_name = 'Póliza contable'
        verbose_name_plural = 'Pólizas contables'
        ordering = ['-fecha_poliza', '-fecha_creacion']


class MovimientoContable(models.Model):
    poliza = models.ForeignKey(
        PolizaContable,
        on_delete=models.CASCADE,
        related_name='movimientos',
    )
    cuenta = models.ForeignKey(
        CuentaContable,
        on_delete=models.PROTECT,
        related_name='movimientos',
    )
    descripcion = models.CharField('Descripción', max_length=255, blank=True)
    debe = models.DecimalField('Debe', max_digits=14, decimal_places=2, default=0)
    haber = models.DecimalField('Haber', max_digits=14, decimal_places=2, default=0)

    def __str__(self):
        return f"{self.poliza.folio} - {self.cuenta.codigo}"

    class Meta:
        verbose_name = 'Movimiento contable'
        verbose_name_plural = 'Movimientos contables'


class EstadoFinanciero(RegistroAuditable):
    class TipoEstado(models.TextChoices):
        RESULTADOS = 'RESULTADOS', 'Estado de resultados'
        BALANCE = 'BALANCE', 'Balance general'
        FLUJO_CAJA = 'FLUJO_CAJA', 'Flujo de caja'

    nombre = models.CharField('Nombre', max_length=150)
    tipo = models.CharField('Tipo', max_length=15, choices=TipoEstado.choices)
    fecha_inicio = models.DateField('Fecha inicio')
    fecha_fin = models.DateField('Fecha fin')
    datos = models.JSONField('Datos', default=dict, blank=True)
    notas = models.TextField('Notas', blank=True)

    def __str__(self):
        return f"{self.nombre} ({self.fecha_inicio} - {self.fecha_fin})"

    class Meta:
        verbose_name = 'Estado financiero'
        verbose_name_plural = 'Estados financieros'
        ordering = ['-fecha_fin', '-fecha_creacion']


class PresupuestoFinanciero(RegistroAuditable):
    class Categoria(models.TextChoices):
        INGRESO = 'INGRESO', 'Ingreso'
        GASTO = 'GASTO', 'Gasto'
        CAPEX = 'CAPEX', 'CAPEX'
        OPEX = 'OPEX', 'OPEX'

    class Periodicidad(models.TextChoices):
        MENSUAL = 'MENSUAL', 'Mensual'
        TRIMESTRAL = 'TRIMESTRAL', 'Trimestral'
        ANUAL = 'ANUAL', 'Anual'

    nombre = models.CharField('Nombre', max_length=150)
    categoria = models.CharField('Categoría', max_length=12, choices=Categoria.choices)
    periodicidad = models.CharField('Periodicidad', max_length=12, choices=Periodicidad.choices, default=Periodicidad.MENSUAL)
    fecha_inicio = models.DateField('Fecha inicio')
    fecha_fin = models.DateField('Fecha fin')
    monto_presupuestado = models.DecimalField('Monto presupuestado', max_digits=14, decimal_places=2)
    monto_real = models.DecimalField('Monto real', max_digits=14, decimal_places=2, default=0)
    descripcion = models.TextField('Descripción', blank=True)
    activo = models.BooleanField('Activo', default=True)

    def __str__(self):
        return self.nombre

    class Meta:
        verbose_name = 'Presupuesto financiero'
        verbose_name_plural = 'Presupuestos financieros'
        ordering = ['-fecha_fin', 'nombre']


class CuentaPorPagarCobrar(RegistroAuditable):
    class TipoCuenta(models.TextChoices):
        POR_PAGAR = 'POR_PAGAR', 'Cuenta por pagar'
        POR_COBRAR = 'POR_COBRAR', 'Cuenta por cobrar'

    class EstadoCuenta(models.TextChoices):
        PENDIENTE = 'PENDIENTE', 'Pendiente'
        PARCIAL = 'PARCIAL', 'Parcial'
        PAGADA = 'PAGADA', 'Pagada / Cobrada'
        VENCIDA = 'VENCIDA', 'Vencida'

    tipo = models.CharField('Tipo', max_length=12, choices=TipoCuenta.choices)
    folio = models.CharField('Folio', max_length=30, unique=True)
    tercero_nombre = models.CharField('Proveedor / Cliente', max_length=200)
    cliente_compra = models.ForeignKey(
        ClienteCompra,
        on_delete=models.PROTECT,
        related_name='cuentas_financieras',
        null=True,
        blank=True,
    )
    proveedor = models.ForeignKey(
        Proveedor,
        on_delete=models.PROTECT,
        related_name='cuentas_financieras',
        null=True,
        blank=True,
    )
    orden_compra = models.ForeignKey(
        OrdenCompra,
        on_delete=models.SET_NULL,
        related_name='cuentas_financieras',
        null=True,
        blank=True,
    )
    monto_total = models.DecimalField('Monto total', max_digits=14, decimal_places=2)
    monto_pagado = models.DecimalField('Monto pagado / cobrado', max_digits=14, decimal_places=2, default=0)
    fecha_emision = models.DateField('Fecha emisión')
    fecha_vencimiento = models.DateField('Fecha vencimiento')
    estado = models.CharField('Estado', max_length=12, choices=EstadoCuenta.choices, default=EstadoCuenta.PENDIENTE)
    observaciones = models.TextField('Observaciones', blank=True)

    def __str__(self):
        return f"{self.folio} - {self.tercero_nombre}"

    class Meta:
        verbose_name = 'Cuenta por pagar / cobrar'
        verbose_name_plural = 'Cuentas por pagar / cobrar'
        ordering = ['fecha_vencimiento', '-fecha_creacion']


class CosteoProduccion(RegistroAuditable):
    class EstadoCosteo(models.TextChoices):
        BORRADOR = 'BORRADOR', 'Borrador'
        CERRADO = 'CERRADO', 'Cerrado'

    orden_fabricacion = models.ForeignKey(
        OrdenFabricacion,
        on_delete=models.CASCADE,
        related_name='costeos_financieros',
        null=True,
        blank=True,
    )
    lote_produccion = models.ForeignKey(
        LoteProduccion,
        on_delete=models.CASCADE,
        related_name='costeos_financieros',
        null=True,
        blank=True,
    )
    costo_material_plan = models.DecimalField('Costo material plan', max_digits=14, decimal_places=2, default=0)
    costo_material_real = models.DecimalField('Costo material real', max_digits=14, decimal_places=2, default=0)
    costo_maquina_real = models.DecimalField('Costo máquina real', max_digits=14, decimal_places=2, default=0)
    costo_operador_real = models.DecimalField('Costo operador real', max_digits=14, decimal_places=2, default=0)
    costo_total_plan = models.DecimalField('Costo total plan', max_digits=14, decimal_places=2, default=0)
    costo_total_real = models.DecimalField('Costo total real', max_digits=14, decimal_places=2, default=0)
    ingreso_estimado = models.DecimalField('Ingreso estimado', max_digits=14, decimal_places=2, default=0)
    rentabilidad = models.DecimalField('Rentabilidad', max_digits=14, decimal_places=2, default=0)
    margen_pct = models.DecimalField('Margen %', max_digits=7, decimal_places=2, default=0)
    estado = models.CharField('Estado', max_length=10, choices=EstadoCosteo.choices, default=EstadoCosteo.BORRADOR)
    detalle = models.JSONField('Detalle', default=dict, blank=True)

    def __str__(self):
        referencia = self.orden_fabricacion.folio if self.orden_fabricacion_id else (self.lote_produccion.folio if self.lote_produccion_id else 'Sin referencia')
        return f"Costeo {referencia}"

    class Meta:
        verbose_name = 'Costeo de producción'
        verbose_name_plural = 'Costeos de producción'
        ordering = ['-fecha_actualizacion']


class ReporteFinanciero(RegistroAuditable):
    class TipoReporte(models.TextChoices):
        KPI = 'KPI', 'KPIs financieros'
        RESULTADOS = 'RESULTADOS', 'Resultados'
        FLUJO_CAJA = 'FLUJO_CAJA', 'Flujo de caja'
        COSTEO = 'COSTEO', 'Costeo de producción'

    nombre = models.CharField('Nombre', max_length=150)
    tipo = models.CharField('Tipo', max_length=15, choices=TipoReporte.choices)
    fecha_inicio = models.DateField('Fecha inicio')
    fecha_fin = models.DateField('Fecha fin')
    indicadores = models.JSONField('Indicadores', default=dict, blank=True)
    alertas = models.JSONField('Alertas', default=list, blank=True)

    def __str__(self):
        return self.nombre

    class Meta:
        verbose_name = 'Reporte financiero'
        verbose_name_plural = 'Reportes financieros'
        ordering = ['-fecha_fin', '-fecha_creacion']


class DeclaracionImpuesto(RegistroAuditable):
    class TipoImpuesto(models.TextChoices):
        IVA = 'IVA', 'IVA'
        ISR = 'ISR', 'ISR'
        IEPS = 'IEPS', 'IEPS'
        LOCAL = 'LOCAL', 'Impuesto local'

    class EstadoDeclaracion(models.TextChoices):
        BORRADOR = 'BORRADOR', 'Borrador'
        CALCULADA = 'CALCULADA', 'Calculada'
        PRESENTADA = 'PRESENTADA', 'Presentada'

    folio = models.CharField('Folio', max_length=30, unique=True)
    tipo_impuesto = models.CharField('Tipo impuesto', max_length=10, choices=TipoImpuesto.choices)
    periodo_inicio = models.DateField('Periodo inicio')
    periodo_fin = models.DateField('Periodo fin')
    base_gravable = models.DecimalField('Base gravable', max_digits=14, decimal_places=2, default=0)
    tasa = models.DecimalField('Tasa %', max_digits=7, decimal_places=2, default=0)
    impuesto_calculado = models.DecimalField('Impuesto calculado', max_digits=14, decimal_places=2, default=0)
    estado = models.CharField('Estado', max_length=12, choices=EstadoDeclaracion.choices, default=EstadoDeclaracion.BORRADOR)
    acuse = models.CharField('Acuse / referencia', max_length=120, blank=True)
    detalle = models.JSONField('Detalle', default=dict, blank=True)

    def __str__(self):
        return self.folio

    class Meta:
        verbose_name = 'Declaración de impuesto'
        verbose_name_plural = 'Declaraciones de impuestos'
        ordering = ['-periodo_fin', '-fecha_creacion']
