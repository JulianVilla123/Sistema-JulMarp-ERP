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
    fecha_orden = models.DateField('Fecha de orden')
    fecha_prometida = models.DateField('Fecha prometida', null=True, blank=True)
    condiciones_pago = models.CharField('Condiciones de pago', max_length=120, blank=True)
    observaciones = models.TextField('Observaciones', blank=True)
    estado = models.CharField(
        'Estado',
        max_length=12,
        choices=EstadoOrden.choices,
        default=EstadoOrden.BORRADOR,
    )
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
