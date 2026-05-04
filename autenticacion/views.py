from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout, get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from datetime import date, datetime, timedelta
from django.db import IntegrityError
from django.db import transaction
from django.db.models import Case, Count, DecimalField, Q, Sum, Value, When
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from collections import defaultdict
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from .models import (
    Almacen,
    BOM,
    BOMDetalle,
    BOMOperacion,
    InventarioAlmacen,
    LoteProduccion,
    Material,
    OrdenCompra,
    OrdenCompraDetalle,
    Proveedor,
    ProveedorMaterialPrecio,
    RecepcionMaterial,
    RecepcionMaterialDetalle,
    OrdenFabricacion,
    OrdenFabricacionDetalle,
    PlanProduccion,
    PlanProduccionDetalle,
    RequerimientoMaterialProduccion,
    RequerimientoMaterialProduccionDetalle,
    SalidaLinea,
    SalidaLineaDetalle,
    TransferenciaAlmacen,
    TransferenciaAlmacenDetalle,
    UsuarioERP,
)

# Create your views here.

User = get_user_model()


ALMACENES_BASE = [
    ('ALM-001', 'Almacen Materia Prima A'),
    ('ALM-002', 'Almacen Materia Prima B'),
    ('ALM-003', 'Almacen Componentes Electronicos'),
    ('ALM-004', 'Almacen Empaque y Consumibles'),
    ('ALM-005', 'Almacen Cuarentena y Retenido'),
]

ORDEN_CONDICIONES_PAGO = [
    'Contado',
    'Crédito 15 días',
    'Crédito 30 días',
    'Crédito 45 días',
    'Crédito 60 días',
    'Anticipo 50% / Contraentrega 50%',
    'Transferencia bancaria',
]


def _ensure_almacenes_base():
    for codigo, nombre in ALMACENES_BASE:
        Almacen.objects.get_or_create(
            codigo=codigo,
            defaults={
                'nombre': nombre,
                'activo': True,
            },
        )


def _to_ascii(value):
    normalized = unicodedata.normalize('NFKD', value or '')
    return ''.join(ch for ch in normalized if not unicodedata.combining(ch))


def _sanitize_username(value):
    username = _to_ascii(value).strip().lower()
    username = re.sub(r'\s+', '_', username)
    username = re.sub(r'[^a-z0-9@.+_-]', '', username)
    username = re.sub(r'_+', '_', username)
    username = username.strip('._-+@')
    return username[:150]


def _build_unique_username(base):
    candidate = (base or 'usuario')[:150]

    if not User.objects.filter(username=candidate).exists():
        return candidate

    suffix = 1
    while True:
        suffix_text = f"_{suffix}"
        trimmed = candidate[:150 - len(suffix_text)]
        unique_candidate = f"{trimmed}{suffix_text}"
        if not User.objects.filter(username=unique_candidate).exists():
            return unique_candidate
        suffix += 1


def _decimal_to_float(value):
    if value is None:
        return 0.0
    return float(value)


def _parse_iso_date(value):
    if not value:
        return None

    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _next_orden_compra_folio():
    current_year = date.today().year
    prefix = f"OC-{current_year}-"
    last_folio = (
        OrdenCompra.objects
        .filter(folio__startswith=prefix)
        .order_by('-id')
        .values_list('folio', flat=True)
        .first()
    )

    seq = 1
    if last_folio:
        match = re.match(rf"^{re.escape(prefix)}(\d+)$", last_folio)
        if match:
            seq = int(match.group(1)) + 1

    return f"{prefix}{seq:04d}"


def _ordenes_transiciones_permitidas(estado_actual):
    return {
        OrdenCompra.EstadoOrden.BORRADOR: {
            OrdenCompra.EstadoOrden.APROBADA,
            OrdenCompra.EstadoOrden.CANCELADA,
        },
        OrdenCompra.EstadoOrden.APROBADA: {
            OrdenCompra.EstadoOrden.ENVIADA,
            OrdenCompra.EstadoOrden.CANCELADA,
        },
        OrdenCompra.EstadoOrden.ENVIADA: {
            OrdenCompra.EstadoOrden.PARCIAL,
            OrdenCompra.EstadoOrden.RECIBIDA,
            OrdenCompra.EstadoOrden.CANCELADA,
        },
        OrdenCompra.EstadoOrden.PARCIAL: {
            OrdenCompra.EstadoOrden.RECIBIDA,
            OrdenCompra.EstadoOrden.CANCELADA,
        },
    }.get(estado_actual, set())


def csrf_failure(request, reason='', template_name=None):
    messages.error(request, 'La sesión o el formulario expiró. Recarga la página y vuelve a intentarlo.')

    referer = request.META.get('HTTP_REFERER')
    if referer and url_has_allowed_host_and_scheme(
        referer,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(referer)

    if request.user.is_authenticated:
        return redirect('home')

    return redirect('login')


@never_cache
def login_usuario(request):
    if request.method == 'POST':
        username_input = (request.POST.get('username') or '').strip()
        password = request.POST.get('password')

        user = authenticate(request, username=username_input, password=password)

        if user is None:
            normalized_username = _sanitize_username(username_input)
            if normalized_username and normalized_username != username_input:
                user = authenticate(request, username=normalized_username, password=password)

        if user is not None:
            login(request, user)
            return redirect('home')  # Redirigir a home después del login
        else:
            messages.error(request, 'Usuario o contraseña incorrectos.')
    return render(request, 'authentication/login.html')


@never_cache
def register_usuario(request):
    if request.method == 'POST':
        username_input = (request.POST.get('username') or '').strip()
        first_name = (request.POST.get('first_name') or '').strip()
        last_name = (request.POST.get('last_name') or '').strip()
        email = (request.POST.get('email') or '').strip()
        telefono = (request.POST.get('telefono') or '').strip()
        numero_empleado = (request.POST.get('numero_empleado') or '').strip()
        password = request.POST.get('password')
        confirm_password = request.POST.get('confirm_password')

        username_base = _sanitize_username(username_input)
        if not username_base:
            username_base = _sanitize_username(f"{first_name}_{last_name}")
        if not username_base:
            username_base = _sanitize_username(numero_empleado)

        username = _build_unique_username(username_base)

        if User.objects.filter(email=email).exists():
            messages.error(request, 'El correo ya está registrado.')
        elif not first_name:
            messages.error(request, 'Debes ingresar al menos un nombre.')
        elif not last_name:
            messages.error(request, 'Debes ingresar al menos un apellido.')
        elif not numero_empleado:
            messages.error(request, 'El número de empleado es obligatorio.')
        elif User.objects.filter(numero_empleado=numero_empleado).exists():
            messages.error(request, 'El número de empleado ya está registrado.')
        elif not password:
            messages.error(request, 'La contraseña es obligatoria.')
        elif password != confirm_password:
            messages.error(request, 'Las contraseñas no coinciden.')
        else:
            try:
                user = User.objects.create_user(
                    username=username,
                    email=email,
                    password=password,
                    first_name=first_name,
                    last_name=last_name,
                )
                user.telefono = telefono
                user.numero_empleado = numero_empleado
                user.save()

                if username_input != username:
                    messages.success(request, f'Usuario asignado automáticamente: {username}')

                messages.success(request, 'Registro exitoso. Ya puedes iniciar sesión.')
                return redirect('login')
            except IntegrityError:
                messages.error(request, 'El número de empleado ya está registrado.')

        form_data = {
            'username': username,
            'first_name': first_name,
            'last_name': last_name,
            'email': email,
            'telefono': telefono,
            'numero_empleado': numero_empleado,
        }
        return render(request, 'authentication/register.html', {'form_data': form_data})

    return render(request, 'authentication/register.html')


@login_required(login_url='login')
def home(request):
    usuario = request.user
    departamento = usuario.departamento.nombre if usuario.departamento else 'Sin asignar'

    context = {
        'usuario': usuario,
        'departamento': departamento,
    }

    if departamento == 'Producción':
        hoy = date.today()
        fecha_inicio_semana = hoy - timedelta(days=6)

        # KPIs principales
        planes_activos = PlanProduccion.objects.filter(
            estado__in=[PlanProduccion.EstadoPlan.APROBADO, PlanProduccion.EstadoPlan.EN_PROCESO]
        ).count()
        ofs_en_proceso = OrdenFabricacion.objects.filter(
            estado=OrdenFabricacion.EstadoOF.EN_PROCESO
        ).count()
        ofs_completadas_semana = OrdenFabricacion.objects.filter(
            estado=OrdenFabricacion.EstadoOF.COMPLETADA,
            fecha_fin_real__date__gte=fecha_inicio_semana,
            fecha_fin_real__date__lte=hoy,
        ).count()
        boms_activos = BOM.objects.filter(activo=True, tipo=BOM.TipoBOM.MFG).count()
        reqs_pendientes = RequerimientoMaterialProduccion.objects.filter(
            estado=RequerimientoMaterialProduccion.EstadoRequerimiento.BORRADOR
        ).count()

        # Eficiencia: cantidad producida vs planificada en OFs completadas recientes
        ofs_completadas = OrdenFabricacion.objects.filter(
            estado=OrdenFabricacion.EstadoOF.COMPLETADA,
            fecha_fin_real__isnull=False,
        ).order_by('-fecha_fin_real')[:20]
        total_plan = sum(of.cantidad_planificada for of in ofs_completadas)
        total_real = sum(of.cantidad_producida for of in ofs_completadas)
        eficiencia = 0
        if total_plan > 0:
            eficiencia = min(int(round(float(total_real / total_plan) * 100)), 100)

        # OFs recientes para actividad
        ofs_recientes_home = (
            OrdenFabricacion.objects
            .select_related('bom', 'plan')
            .order_by('-fecha_actualizacion')[:6]
        )

        # Planes recientes
        planes_recientes_home = (
            PlanProduccion.objects
            .select_related('bom')
            .order_by('-fecha_creacion')[:5]
        )

        # Distribución de estados de OFs para gráfica
        estados_of = {
            'BORRADOR': OrdenFabricacion.objects.filter(estado=OrdenFabricacion.EstadoOF.BORRADOR).count(),
            'EN_PROCESO': ofs_en_proceso,
            'PAUSADA': OrdenFabricacion.objects.filter(estado=OrdenFabricacion.EstadoOF.PAUSADA).count(),
            'COMPLETADA': OrdenFabricacion.objects.filter(estado=OrdenFabricacion.EstadoOF.COMPLETADA).count(),
            'CANCELADA': OrdenFabricacion.objects.filter(estado=OrdenFabricacion.EstadoOF.CANCELADA).count(),
        }

        # Producción por producto (top 5 OFs completadas)
        prod_por_producto = {}
        for of in OrdenFabricacion.objects.filter(
            estado=OrdenFabricacion.EstadoOF.COMPLETADA
        ).select_related('bom').order_by('-fecha_fin_real')[:50]:
            key = of.bom.producto
            prod_por_producto[key] = float(prod_por_producto.get(key, 0)) + float(of.cantidad_producida)
        top_productos = sorted(prod_por_producto.items(), key=lambda x: x[1], reverse=True)[:5]

        context.update({
            'prod_planes_activos': planes_activos,
            'prod_ofs_en_proceso': ofs_en_proceso,
            'prod_ofs_completadas_semana': ofs_completadas_semana,
            'prod_boms_activos': boms_activos,
            'prod_reqs_pendientes': reqs_pendientes,
            'prod_eficiencia': eficiencia,
            'prod_ofs_recientes': ofs_recientes_home,
            'prod_planes_recientes': planes_recientes_home,
            'prod_chart_data': {
                'estados_labels': list(estados_of.keys()),
                'estados_values': list(estados_of.values()),
                'top_productos_labels': [p[0] for p in top_productos],
                'top_productos_values': [p[1] for p in top_productos],
            },
        })

    if departamento in {'Inventario', 'Admin'}:
        fecha_fin = date.today()
        fecha_inicio = fecha_fin - timedelta(days=6)

        resumen_stock = InventarioAlmacen.objects.filter(stock_actual__gt=0).aggregate(
            total_stock=Sum('stock_actual'),
        )
        materiales_activos = Material.objects.filter(activo=True).count()
        almacenes_activos = Almacen.objects.filter(activo=True).count()

        entradas_semana = RecepcionMaterialDetalle.objects.filter(
            recepcion__fecha_recepcion__gte=fecha_inicio,
            recepcion__fecha_recepcion__lte=fecha_fin,
        ).aggregate(total=Sum('cantidad_recibida'))

        salidas_semana = SalidaLineaDetalle.objects.filter(
            salida__fecha_salida__gte=fecha_inicio,
            salida__fecha_salida__lte=fecha_fin,
        ).aggregate(total=Sum('cantidad_enviada'))

        transferencias_semana = TransferenciaAlmacenDetalle.objects.filter(
            transferencia__fecha_transferencia__gte=fecha_inicio,
            transferencia__fecha_transferencia__lte=fecha_fin,
        ).aggregate(total=Sum('cantidad_transferida'))

        top_materiales = list(
            InventarioAlmacen.objects.filter(stock_actual__gt=0)
            .values('material__sku', 'material__nombre')
            .annotate(stock_total=Sum('stock_actual'))
            .order_by('-stock_total')[:5]
        )

        chart_top_labels = [f"{item['material__sku']}" for item in top_materiales]
        chart_top_values = [float(item['stock_total'] or 0) for item in top_materiales]

        chart_flujo_labels = ['Entradas', 'Salidas a linea', 'Transferencias']
        chart_flujo_values = [
            float(entradas_semana.get('total') or 0),
            float(salidas_semana.get('total') or 0),
            float(transferencias_semana.get('total') or 0),
        ]

        movimientos_recientes = []
        for detalle in RecepcionMaterialDetalle.objects.select_related('recepcion').order_by('-recepcion__fecha_recepcion', '-id')[:3]:
            movimientos_recientes.append({
                'fecha': detalle.recepcion.fecha_recepcion,
                'tipo': 'ENTRADA',
                'descripcion': f"{detalle.sku} {detalle.descripcion}",
                'cantidad': detalle.cantidad_recibida,
                'referencia': f"REC-{detalle.recepcion_id}",
            })

        for detalle in SalidaLineaDetalle.objects.select_related('salida').order_by('-salida__fecha_salida', '-id')[:3]:
            movimientos_recientes.append({
                'fecha': detalle.salida.fecha_salida,
                'tipo': 'SALIDA_LINEA',
                'descripcion': f"{detalle.sku} {detalle.descripcion}",
                'cantidad': detalle.cantidad_enviada,
                'referencia': f"SAL-{detalle.salida_id}",
            })

        for detalle in TransferenciaAlmacenDetalle.objects.select_related('transferencia').order_by('-transferencia__fecha_transferencia', '-id')[:3]:
            movimientos_recientes.append({
                'fecha': detalle.transferencia.fecha_transferencia,
                'tipo': 'TRANSFERENCIA',
                'descripcion': f"{detalle.sku} {detalle.descripcion}",
                'cantidad': detalle.cantidad_transferida,
                'referencia': detalle.transferencia.referencia or f"TRF-{detalle.transferencia_id}",
            })

        movimientos_recientes.sort(key=lambda x: (x['fecha'], x['referencia']), reverse=True)

        context.update({
            'inv_total_stock': resumen_stock.get('total_stock') or Decimal('0'),
            'inv_materiales_activos': materiales_activos,
            'inv_almacenes_activos': almacenes_activos,
            'inv_entradas_semana': entradas_semana.get('total') or Decimal('0'),
            'inv_salidas_semana': salidas_semana.get('total') or Decimal('0'),
            'inv_transferencias_semana': transferencias_semana.get('total') or Decimal('0'),
            'inv_top_materiales': top_materiales,
            'inv_movimientos_recientes': movimientos_recientes[:6],
            'inv_chart_data': {
                'top_labels': chart_top_labels,
                'top_values': chart_top_values,
                'flujo_labels': chart_flujo_labels,
                'flujo_values': chart_flujo_values,
            },
        })

    return render(request, 'home.html', context)


def logout_usuario(request):
    logout(request)
    return redirect('login')


@login_required(login_url='login')
def perfil_usuario(request):
    return render(request, 'authentication/perfil.html', {'usuario': request.user})


@login_required(login_url='login')
@never_cache
def entrada_material_planta(request):
    _ensure_almacenes_base()
    recepciones_recientes = RecepcionMaterial.objects.filter(creado_por=request.user)[:5]
    materiales_catalogo = Material.objects.filter(activo=True).order_by('sku')[:1000]
    proveedores_catalogo = Proveedor.objects.filter(activo=True).order_by('nombre')
    almacenes_catalogo = Almacen.objects.filter(activo=True).order_by('codigo')
    almacenes_por_codigo = {almacen.codigo.upper(): almacen for almacen in almacenes_catalogo}
    materiales_catalogo_json = {
        material.sku.upper(): {
            'descripcion': material.nombre,
            'um': material.um,
        }
        for material in materiales_catalogo
    }

    if request.method == 'POST':
        accion = (request.POST.get('accion') or '').strip()
        fecha_recepcion = (request.POST.get('fecha_recepcion') or '').strip()
        hora_recepcion = (request.POST.get('hora_recepcion') or '').strip()
        proveedor_id = (request.POST.get('proveedor') or '').strip()
        orden_compra = (request.POST.get('orden_compra') or '').strip()
        factura = (request.POST.get('factura') or '').strip()
        transportista = (request.POST.get('transportista') or '').strip()
        placas = (request.POST.get('placas') or '').strip()
        observaciones = (request.POST.get('observaciones') or '').strip()
        accion_recomendada = (request.POST.get('accion_recomendada') or '').strip()

        skus = request.POST.getlist('sku[]')
        descripciones = request.POST.getlist('descripcion[]')
        ums = request.POST.getlist('um[]')
        cantidades_oc = request.POST.getlist('cantidad_oc[]')
        cantidades_recibidas = request.POST.getlist('cantidad_recibida[]')
        lotes = request.POST.getlist('lote_material[]')
        ubicaciones = request.POST.getlist('ubicacion_destino[]')
        estatuses = request.POST.getlist('estatus_material[]')

        proveedor_obj = Proveedor.objects.filter(id=proveedor_id, activo=True).first() if proveedor_id else None

        if not fecha_recepcion or not hora_recepcion or not proveedor_obj:
            messages.error(request, 'Fecha, hora y proveedor son obligatorios y deben seleccionarse del catálogo.')
            return render(
                request,
                'inventario/entrada_planta.html',
                {
                    'recepciones_recientes': recepciones_recientes,
                    'materiales_catalogo': materiales_catalogo,
                    'proveedores_catalogo': proveedores_catalogo,
                    'almacenes_catalogo': almacenes_catalogo,
                    'materiales_catalogo_json': materiales_catalogo_json,
                },
            )

        materiales_permitidos = {
            material.sku.upper(): material
            for material in proveedor_obj.materiales.filter(activo=True)
        }

        materiales = []
        total_rows = max(
            len(skus),
            len(descripciones),
            len(ums),
            len(cantidades_oc),
            len(cantidades_recibidas),
            len(lotes),
            len(ubicaciones),
            len(estatuses),
        )

        for idx in range(total_rows):
            sku = (skus[idx] if idx < len(skus) else '').strip().upper()
            descripcion = (descripciones[idx] if idx < len(descripciones) else '').strip()
            um = (ums[idx] if idx < len(ums) else '').strip()
            cantidad_oc = (cantidades_oc[idx] if idx < len(cantidades_oc) else '0').strip()
            cantidad_recibida = (cantidades_recibidas[idx] if idx < len(cantidades_recibidas) else '0').strip()
            lote = (lotes[idx] if idx < len(lotes) else '').strip()
            ubicacion_destino = (ubicaciones[idx] if idx < len(ubicaciones) else '').strip().upper()
            estatus_material = (estatuses[idx] if idx < len(estatuses) else '').strip() or RecepcionMaterialDetalle.EstatusDetalle.ACEPTADO

            row_has_data = any([
                sku,
                descripcion,
                um,
                cantidad_oc not in ['', '0', '0.0', '0.00'],
                cantidad_recibida not in ['', '0', '0.0', '0.00'],
                lote,
                ubicacion_destino,
            ])

            if not row_has_data:
                continue

            if not sku or not descripcion:
                messages.error(request, f'En la fila {idx + 1}, SKU y descripción son obligatorios.')
                return render(
                    request,
                    'inventario/entrada_planta.html',
                    {
                        'recepciones_recientes': recepciones_recientes,
                        'materiales_catalogo': materiales_catalogo,
                        'proveedores_catalogo': proveedores_catalogo,
                        'almacenes_catalogo': almacenes_catalogo,
                        'materiales_catalogo_json': materiales_catalogo_json,
                    },
                )

            if not ubicacion_destino:
                messages.error(request, f'En la fila {idx + 1}, debes seleccionar un almacen destino.')
                return render(
                    request,
                    'inventario/entrada_planta.html',
                    {
                        'recepciones_recientes': recepciones_recientes,
                        'materiales_catalogo': materiales_catalogo,
                        'proveedores_catalogo': proveedores_catalogo,
                        'almacenes_catalogo': almacenes_catalogo,
                        'materiales_catalogo_json': materiales_catalogo_json,
                    },
                )

            if sku not in materiales_permitidos:
                messages.error(request, f'En la fila {idx + 1}, el material seleccionado no pertenece al proveedor elegido.')
                return render(
                    request,
                    'inventario/entrada_planta.html',
                    {
                        'recepciones_recientes': recepciones_recientes,
                        'materiales_catalogo': materiales_catalogo,
                        'proveedores_catalogo': proveedores_catalogo,
                        'almacenes_catalogo': almacenes_catalogo,
                        'materiales_catalogo_json': materiales_catalogo_json,
                    },
                )

            if ubicacion_destino and ubicacion_destino not in almacenes_por_codigo:
                messages.error(request, f'En la fila {idx + 1}, selecciona un almacen valido del catalogo.')
                return render(
                    request,
                    'inventario/entrada_planta.html',
                    {
                        'recepciones_recientes': recepciones_recientes,
                        'materiales_catalogo': materiales_catalogo,
                        'proveedores_catalogo': proveedores_catalogo,
                        'almacenes_catalogo': almacenes_catalogo,
                        'materiales_catalogo_json': materiales_catalogo_json,
                    },
                )

            try:
                cantidad_oc_decimal = Decimal((cantidad_oc or '0').replace(',', '.'))
                cantidad_recibida_decimal = Decimal((cantidad_recibida or '0').replace(',', '.'))
            except InvalidOperation:
                messages.error(request, f'En la fila {idx + 1}, las cantidades deben ser numéricas válidas.')
                return render(
                    request,
                    'inventario/entrada_planta.html',
                    {
                        'recepciones_recientes': recepciones_recientes,
                        'materiales_catalogo': materiales_catalogo,
                        'proveedores_catalogo': proveedores_catalogo,
                        'almacenes_catalogo': almacenes_catalogo,
                        'materiales_catalogo_json': materiales_catalogo_json,
                    },
                )

            materiales.append({
                'sku': sku,
                'descripcion': descripcion,
                'um': um,
                'cantidad_oc': cantidad_oc_decimal,
                'cantidad_recibida': cantidad_recibida_decimal,
                'lote': lote,
                'ubicacion_destino': ubicacion_destino,
                'estatus': estatus_material,
            })

        if accion != 'borrador' and not materiales:
            messages.error(request, 'Para guardar recepción debes capturar al menos un material válido.')
            return render(
                request,
                'inventario/entrada_planta.html',
                {
                    'recepciones_recientes': recepciones_recientes,
                    'materiales_catalogo': materiales_catalogo,
                    'proveedores_catalogo': proveedores_catalogo,
                    'almacenes_catalogo': almacenes_catalogo,
                    'materiales_catalogo_json': materiales_catalogo_json,
                },
            )

        with transaction.atomic():
            recepcion = RecepcionMaterial.objects.create(
                fecha_recepcion=fecha_recepcion,
                hora_recepcion=hora_recepcion,
                proveedor=proveedor_obj.nombre,
                proveedor_registrado=proveedor_obj,
                orden_compra=orden_compra,
                factura=factura,
                transportista=transportista,
                placas=placas,
                chk_oc=bool(request.POST.get('chk_oc')),
                chk_cantidad=bool(request.POST.get('chk_cantidad')),
                chk_empaque=bool(request.POST.get('chk_empaque')),
                chk_lote=bool(request.POST.get('chk_lote')),
                chk_vigencia=bool(request.POST.get('chk_vigencia')),
                chk_certificado=bool(request.POST.get('chk_certificado')),
                chk_estado_fisico=bool(request.POST.get('chk_estado_fisico')),
                chk_foto=bool(request.POST.get('chk_foto')),
                chk_calidad=bool(request.POST.get('chk_calidad')),
                observaciones=observaciones,
                accion_recomendada=accion_recomendada,
                estado=(
                    RecepcionMaterial.EstadoRecepcion.BORRADOR
                    if accion == 'borrador'
                    else RecepcionMaterial.EstadoRecepcion.ENVIADA
                ),
                creado_por=request.user,
            )

            for item in materiales:
                material = materiales_permitidos[item['sku']]

                material.stock_actual = (material.stock_actual or 0) + item['cantidad_recibida']
                material.save(update_fields=['stock_actual', 'fecha_actualizacion'])

                almacen_obj = almacenes_por_codigo.get(item['ubicacion_destino'])
                if almacen_obj:
                    inventario_almacen, _ = InventarioAlmacen.objects.get_or_create(
                        material=material,
                        almacen=almacen_obj,
                        lote=item['lote'],
                        defaults={'stock_actual': 0},
                    )
                    inventario_almacen.stock_actual = (inventario_almacen.stock_actual or 0) + item['cantidad_recibida']
                    inventario_almacen.save(update_fields=['stock_actual', 'fecha_actualizacion'])

                RecepcionMaterialDetalle.objects.create(
                    recepcion=recepcion,
                    material=material,
                    sku=item['sku'],
                    descripcion=item['descripcion'],
                    um=item['um'],
                    cantidad_oc=item['cantidad_oc'],
                    cantidad_recibida=item['cantidad_recibida'],
                    lote=item['lote'],
                    ubicacion_destino=item['ubicacion_destino'],
                    estatus=item['estatus'],
                )

        if accion == 'borrador':
            messages.success(request, f'Borrador guardado con folio {recepcion.id}.')
        else:
            messages.success(request, f'Recepción guardada con folio {recepcion.id}.')

        return redirect('entrada_planta')

    return render(
        request,
        'inventario/entrada_planta.html',
        {
            'recepciones_recientes': recepciones_recientes,
            'materiales_catalogo': materiales_catalogo,
            'proveedores_catalogo': proveedores_catalogo,
            'almacenes_catalogo': almacenes_catalogo,
            'materiales_catalogo_json': materiales_catalogo_json,
        },
    )


@login_required(login_url='login')
@never_cache
def entrada_material_linea(request):
    _ensure_almacenes_base()
    lineas_destino_permitidas = {
        'Línea SMT-01',
        'Línea SMT-02',
        'Línea SMT-03',
    }

    salidas_recientes = SalidaLinea.objects.filter(creado_por=request.user).prefetch_related('detalles')[:5]
    almacenes_catalogo = Almacen.objects.filter(activo=True).order_by('codigo')
    inventarios_con_stock = InventarioAlmacen.objects.filter(
        almacen__activo=True,
        material__activo=True,
        stock_actual__gt=0,
    ).select_related('almacen', 'material').order_by('almacen__codigo', 'material__sku')

    inventario_catalogo_json = {}
    for inventario in inventarios_con_stock:
        almacen_data = inventario_catalogo_json.setdefault(inventario.almacen.codigo, {})
        material_data = almacen_data.setdefault(
            inventario.material.sku,
            {
                'descripcion': inventario.material.nombre,
                'um': inventario.material.um,
                'lotes': [],
            },
        )
        material_data['lotes'].append({
            'lote': inventario.lote,
            'lote_label': inventario.lote or 'Sin lote registrado',
            'stock': float(inventario.stock_actual),
        })

    materiales_catalogo = {}
    for inventario in inventarios_con_stock:
        materiales_catalogo[inventario.material.sku] = {
            'descripcion': inventario.material.nombre,
            'um': inventario.material.um,
        }

    if request.method == 'POST':
        fecha_salida = (request.POST.get('fecha_salida') or '').strip()
        hora_salida = (request.POST.get('hora_salida') or '').strip()
        linea_destino = (request.POST.get('linea_destino') or '').strip()
        orden_produccion = (request.POST.get('orden_produccion') or '').strip()
        turno = (request.POST.get('turno') or '').strip()
        observaciones = (request.POST.get('observaciones') or '').strip()

        almacenes_origen = request.POST.getlist('almacen_origen[]')
        skus = request.POST.getlist('sku[]')
        descripciones = request.POST.getlist('descripcion[]')
        ums = request.POST.getlist('um[]')
        cantidades = request.POST.getlist('cantidad_enviada[]')
        lotes = request.POST.getlist('lote[]')

        if not fecha_salida or not hora_salida or not linea_destino:
            messages.error(request, 'Fecha, hora y linea destino son obligatorios.')
            return render(
                request,
                'inventario/entrada_linea.html',
                {
                    'salidas_recientes': salidas_recientes,
                    'almacenes_catalogo': almacenes_catalogo,
                    'inventario_catalogo_json': dict(inventario_catalogo_json),
                    'materiales_catalogo': materiales_catalogo,
                },
            )

        if linea_destino not in lineas_destino_permitidas:
            messages.error(request, 'La linea destino seleccionada no es valida.')
            return render(
                request,
                'inventario/entrada_linea.html',
                {
                    'salidas_recientes': salidas_recientes,
                    'almacenes_catalogo': almacenes_catalogo,
                    'inventario_catalogo_json': dict(inventario_catalogo_json),
                    'materiales_catalogo': materiales_catalogo,
                },
            )

        movimientos = []
        total_rows = max(len(almacenes_origen), len(skus), len(descripciones), len(ums), len(cantidades), len(lotes))

        for idx in range(total_rows):
            almacen_codigo = (almacenes_origen[idx] if idx < len(almacenes_origen) else '').strip().upper()
            sku = (skus[idx] if idx < len(skus) else '').strip().upper()
            descripcion = (descripciones[idx] if idx < len(descripciones) else '').strip()
            um = (ums[idx] if idx < len(ums) else '').strip()
            cantidad_texto = (cantidades[idx] if idx < len(cantidades) else '').strip()
            lote = (lotes[idx] if idx < len(lotes) else '').strip()

            row_has_data = any([almacen_codigo, sku, descripcion, um, cantidad_texto, lote])
            if not row_has_data:
                continue

            if not almacen_codigo or not sku or not cantidad_texto:
                messages.error(request, f'En la fila {idx + 1}, almacen, SKU y cantidad son obligatorios.')
                return render(
                    request,
                    'inventario/entrada_linea.html',
                    {
                        'salidas_recientes': salidas_recientes,
                        'almacenes_catalogo': almacenes_catalogo,
                        'inventario_catalogo_json': dict(inventario_catalogo_json),
                        'materiales_catalogo': materiales_catalogo,
                    },
                )

            lote_registrado = None
            almacen_catalogo = inventario_catalogo_json.get(almacen_codigo, {})
            material_catalogo = almacen_catalogo.get(sku, {})
            for lote_info in material_catalogo.get('lotes', []):
                if lote_info['lote'] == lote:
                    lote_registrado = lote_info
                    break

            if lote_registrado is None:
                messages.error(request, f'En la fila {idx + 1}, debes seleccionar un lote valido del inventario recibido.')
                return render(
                    request,
                    'inventario/entrada_linea.html',
                    {
                        'salidas_recientes': salidas_recientes,
                        'almacenes_catalogo': almacenes_catalogo,
                        'inventario_catalogo_json': inventario_catalogo_json,
                        'materiales_catalogo': materiales_catalogo,
                    },
                )

            try:
                cantidad_enviada = Decimal(cantidad_texto.replace(',', '.'))
            except InvalidOperation:
                messages.error(request, f'En la fila {idx + 1}, la cantidad debe ser numerica valida.')
                return render(
                    request,
                    'inventario/entrada_linea.html',
                    {
                        'salidas_recientes': salidas_recientes,
                        'almacenes_catalogo': almacenes_catalogo,
                        'inventario_catalogo_json': dict(inventario_catalogo_json),
                        'materiales_catalogo': materiales_catalogo,
                    },
                )

            if cantidad_enviada <= 0:
                messages.error(request, f'En la fila {idx + 1}, la cantidad debe ser mayor a cero.')
                return render(
                    request,
                    'inventario/entrada_linea.html',
                    {
                        'salidas_recientes': salidas_recientes,
                        'almacenes_catalogo': almacenes_catalogo,
                        'inventario_catalogo_json': dict(inventario_catalogo_json),
                        'materiales_catalogo': materiales_catalogo,
                    },
                )

            movimientos.append({
                'almacen_codigo': almacen_codigo,
                'sku': sku,
                'descripcion': descripcion,
                'um': um,
                'cantidad_enviada': cantidad_enviada,
                'lote': lote,
            })

        if not movimientos:
            messages.error(request, 'Debes capturar al menos un material para enviar a linea.')
            return render(
                request,
                'inventario/entrada_linea.html',
                {
                    'salidas_recientes': salidas_recientes,
                    'almacenes_catalogo': almacenes_catalogo,
                    'inventario_catalogo_json': dict(inventario_catalogo_json),
                    'materiales_catalogo': materiales_catalogo,
                },
            )

        try:
            with transaction.atomic():
                salida = SalidaLinea.objects.create(
                    fecha_salida=fecha_salida,
                    hora_salida=hora_salida,
                    linea_destino=linea_destino,
                    orden_produccion=orden_produccion,
                    turno=turno,
                    observaciones=observaciones,
                    creado_por=request.user,
                )

                for idx, movimiento in enumerate(movimientos, start=1):
                    inventario = InventarioAlmacen.objects.select_for_update().select_related('material', 'almacen').filter(
                        almacen__codigo=movimiento['almacen_codigo'],
                        material__sku=movimiento['sku'],
                        lote=movimiento['lote'],
                    ).first()

                    if not inventario or inventario.stock_actual < movimiento['cantidad_enviada']:
                        raise ValueError(f'En la fila {idx}, no hay stock suficiente del lote seleccionado en el almacen indicado.')

                    inventario.stock_actual = (inventario.stock_actual or 0) - movimiento['cantidad_enviada']
                    inventario.save(update_fields=['stock_actual', 'fecha_actualizacion'])

                    material = inventario.material
                    material.stock_actual = max(Decimal('0'), (material.stock_actual or 0) - movimiento['cantidad_enviada'])
                    material.save(update_fields=['stock_actual', 'fecha_actualizacion'])

                    SalidaLineaDetalle.objects.create(
                        salida=salida,
                        almacen_origen=inventario.almacen,
                        material=material,
                        sku=material.sku,
                        descripcion=movimiento['descripcion'] or material.nombre,
                        um=movimiento['um'] or material.um,
                        cantidad_enviada=movimiento['cantidad_enviada'],
                        lote=inventario.lote,
                    )
        except ValueError as exc:
            messages.error(request, str(exc))
            return render(
                request,
                'inventario/entrada_linea.html',
                {
                    'salidas_recientes': salidas_recientes,
                    'almacenes_catalogo': almacenes_catalogo,
                    'inventario_catalogo_json': dict(inventario_catalogo_json),
                    'materiales_catalogo': materiales_catalogo,
                },
            )

        messages.success(request, f'Salida a linea registrada con folio {salida.id}.')
        return redirect('entrada_linea')

    return render(
        request,
        'inventario/entrada_linea.html',
        {
            'salidas_recientes': salidas_recientes,
            'almacenes_catalogo': almacenes_catalogo,
            'inventario_catalogo_json': dict(inventario_catalogo_json),
            'materiales_catalogo': materiales_catalogo,
        },
    )


@login_required(login_url='login')
@never_cache
def transferencia_almacenes(request):
    _ensure_almacenes_base()

    transferencias_recientes = TransferenciaAlmacen.objects.filter(creado_por=request.user).prefetch_related('detalles')[:5]
    almacenes_catalogo = Almacen.objects.filter(activo=True).order_by('codigo')
    inventarios_con_stock = InventarioAlmacen.objects.filter(
        almacen__activo=True,
        material__activo=True,
        stock_actual__gt=0,
    ).select_related('almacen', 'material').order_by('almacen__codigo', 'material__sku')

    inventario_catalogo_json = {}
    for inventario in inventarios_con_stock:
        almacen_data = inventario_catalogo_json.setdefault(inventario.almacen.codigo, {})
        material_data = almacen_data.setdefault(
            inventario.material.sku,
            {
                'descripcion': inventario.material.nombre,
                'um': inventario.material.um,
                'lotes': [],
            },
        )
        material_data['lotes'].append({
            'lote': inventario.lote,
            'lote_label': inventario.lote or 'Sin lote registrado',
            'stock': float(inventario.stock_actual),
        })

    if request.method == 'POST':
        fecha_transferencia = (request.POST.get('fecha_transferencia') or '').strip()
        hora_transferencia = (request.POST.get('hora_transferencia') or '').strip()
        almacen_origen_codigo = (request.POST.get('almacen_origen') or '').strip().upper()
        almacen_destino_codigo = (request.POST.get('almacen_destino') or '').strip().upper()
        motivo = (request.POST.get('motivo') or '').strip()

        skus = request.POST.getlist('sku[]')
        descripciones = request.POST.getlist('descripcion[]')
        ums = request.POST.getlist('um[]')
        lotes = request.POST.getlist('lote[]')
        cantidades = request.POST.getlist('cantidad_transferida[]')

        if not fecha_transferencia or not hora_transferencia:
            messages.error(request, 'Fecha y hora de transferencia son obligatorias.')
            return render(
                request,
                'inventario/transferencias_almacenes.html',
                {
                    'transferencias_recientes': transferencias_recientes,
                    'almacenes_catalogo': almacenes_catalogo,
                    'inventario_catalogo_json': dict(inventario_catalogo_json),
                },
            )

        fecha_transferencia_obj = _parse_iso_date(fecha_transferencia)
        if not fecha_transferencia_obj:
            messages.error(request, 'La fecha de transferencia no es valida.')
            return render(
                request,
                'inventario/transferencias_almacenes.html',
                {
                    'transferencias_recientes': transferencias_recientes,
                    'almacenes_catalogo': almacenes_catalogo,
                    'inventario_catalogo_json': dict(inventario_catalogo_json),
                },
            )

        if not almacen_origen_codigo or not almacen_destino_codigo:
            messages.error(request, 'Debes seleccionar almacen origen y destino.')
            return render(
                request,
                'inventario/transferencias_almacenes.html',
                {
                    'transferencias_recientes': transferencias_recientes,
                    'almacenes_catalogo': almacenes_catalogo,
                    'inventario_catalogo_json': dict(inventario_catalogo_json),
                },
            )

        if almacen_origen_codigo == almacen_destino_codigo:
            messages.error(request, 'El almacen destino debe ser diferente al almacen origen.')
            return render(
                request,
                'inventario/transferencias_almacenes.html',
                {
                    'transferencias_recientes': transferencias_recientes,
                    'almacenes_catalogo': almacenes_catalogo,
                    'inventario_catalogo_json': dict(inventario_catalogo_json),
                },
            )

        almacen_origen = Almacen.objects.filter(codigo=almacen_origen_codigo, activo=True).first()
        almacen_destino = Almacen.objects.filter(codigo=almacen_destino_codigo, activo=True).first()
        if not almacen_origen or not almacen_destino:
            messages.error(request, 'Selecciona almacenes validos del catalogo.')
            return render(
                request,
                'inventario/transferencias_almacenes.html',
                {
                    'transferencias_recientes': transferencias_recientes,
                    'almacenes_catalogo': almacenes_catalogo,
                    'inventario_catalogo_json': dict(inventario_catalogo_json),
                },
            )

        movimientos = []
        total_rows = max(len(skus), len(descripciones), len(ums), len(lotes), len(cantidades))

        for idx in range(total_rows):
            sku = (skus[idx] if idx < len(skus) else '').strip().upper()
            descripcion = (descripciones[idx] if idx < len(descripciones) else '').strip()
            um = (ums[idx] if idx < len(ums) else '').strip()
            lote = (lotes[idx] if idx < len(lotes) else '').strip()
            cantidad_texto = (cantidades[idx] if idx < len(cantidades) else '').strip()

            row_has_data = any([sku, descripcion, um, lote, cantidad_texto])
            if not row_has_data:
                continue

            if not sku or not cantidad_texto:
                messages.error(request, f'En la fila {idx + 1}, SKU y cantidad son obligatorios.')
                return render(
                    request,
                    'inventario/transferencias_almacenes.html',
                    {
                        'transferencias_recientes': transferencias_recientes,
                        'almacenes_catalogo': almacenes_catalogo,
                        'inventario_catalogo_json': dict(inventario_catalogo_json),
                    },
                )

            lote_registrado = None
            material_catalogo = (inventario_catalogo_json.get(almacen_origen_codigo, {}) or {}).get(sku, {})
            for lote_info in material_catalogo.get('lotes', []):
                if lote_info['lote'] == lote:
                    lote_registrado = lote_info
                    break

            if lote_registrado is None:
                messages.error(request, f'En la fila {idx + 1}, debes seleccionar un lote valido del almacen origen.')
                return render(
                    request,
                    'inventario/transferencias_almacenes.html',
                    {
                        'transferencias_recientes': transferencias_recientes,
                        'almacenes_catalogo': almacenes_catalogo,
                        'inventario_catalogo_json': dict(inventario_catalogo_json),
                    },
                )

            try:
                cantidad_transferida = Decimal(cantidad_texto.replace(',', '.'))
            except InvalidOperation:
                messages.error(request, f'En la fila {idx + 1}, la cantidad debe ser numerica valida.')
                return render(
                    request,
                    'inventario/transferencias_almacenes.html',
                    {
                        'transferencias_recientes': transferencias_recientes,
                        'almacenes_catalogo': almacenes_catalogo,
                        'inventario_catalogo_json': dict(inventario_catalogo_json),
                    },
                )

            if cantidad_transferida <= 0:
                messages.error(request, f'En la fila {idx + 1}, la cantidad debe ser mayor a cero.')
                return render(
                    request,
                    'inventario/transferencias_almacenes.html',
                    {
                        'transferencias_recientes': transferencias_recientes,
                        'almacenes_catalogo': almacenes_catalogo,
                        'inventario_catalogo_json': dict(inventario_catalogo_json),
                    },
                )

            movimientos.append({
                'sku': sku,
                'descripcion': descripcion,
                'um': um,
                'lote': lote,
                'cantidad_transferida': cantidad_transferida,
            })

        if not movimientos:
            messages.error(request, 'Debes capturar al menos un material para transferir.')
            return render(
                request,
                'inventario/transferencias_almacenes.html',
                {
                    'transferencias_recientes': transferencias_recientes,
                    'almacenes_catalogo': almacenes_catalogo,
                    'inventario_catalogo_json': dict(inventario_catalogo_json),
                },
            )

        try:
            with transaction.atomic():
                transferencia = TransferenciaAlmacen.objects.create(
                    fecha_transferencia=fecha_transferencia_obj,
                    hora_transferencia=hora_transferencia,
                    almacen_origen=almacen_origen,
                    almacen_destino=almacen_destino,
                    motivo=motivo,
                    creado_por=request.user,
                )
                transferencia.referencia = f"TRF-{fecha_transferencia_obj.year}-{transferencia.id:04d}"
                transferencia.save(update_fields=['referencia'])

                for idx, movimiento in enumerate(movimientos, start=1):
                    inventario_origen = (
                        InventarioAlmacen.objects
                        .select_for_update()
                        .select_related('material', 'almacen')
                        .filter(
                            almacen=almacen_origen,
                            material__sku=movimiento['sku'],
                            lote=movimiento['lote'],
                        )
                        .first()
                    )

                    if not inventario_origen or inventario_origen.stock_actual < movimiento['cantidad_transferida']:
                        raise ValueError(f'En la fila {idx}, no hay stock suficiente en el almacen origen para ese lote.')

                    inventario_origen.stock_actual = (inventario_origen.stock_actual or 0) - movimiento['cantidad_transferida']
                    inventario_origen.save(update_fields=['stock_actual', 'fecha_actualizacion'])

                    inventario_destino, _ = InventarioAlmacen.objects.select_for_update().get_or_create(
                        almacen=almacen_destino,
                        material=inventario_origen.material,
                        lote=inventario_origen.lote,
                        defaults={'stock_actual': 0},
                    )
                    inventario_destino.stock_actual = (inventario_destino.stock_actual or 0) + movimiento['cantidad_transferida']
                    inventario_destino.save(update_fields=['stock_actual', 'fecha_actualizacion'])

                    TransferenciaAlmacenDetalle.objects.create(
                        transferencia=transferencia,
                        material=inventario_origen.material,
                        sku=inventario_origen.material.sku,
                        descripcion=movimiento['descripcion'] or inventario_origen.material.nombre,
                        um=movimiento['um'] or inventario_origen.material.um,
                        cantidad_transferida=movimiento['cantidad_transferida'],
                        lote=inventario_origen.lote,
                    )
        except ValueError as exc:
            messages.error(request, str(exc))
            return render(
                request,
                'inventario/transferencias_almacenes.html',
                {
                    'transferencias_recientes': transferencias_recientes,
                    'almacenes_catalogo': almacenes_catalogo,
                    'inventario_catalogo_json': dict(inventario_catalogo_json),
                },
            )

        messages.success(request, f'Transferencia registrada con folio {transferencia.referencia}.')
        return redirect('transferencias_almacenes')

    return render(
        request,
        'inventario/transferencias_almacenes.html',
        {
            'transferencias_recientes': transferencias_recientes,
            'almacenes_catalogo': almacenes_catalogo,
            'inventario_catalogo_json': dict(inventario_catalogo_json),
        },
    )


@login_required(login_url='login')
@never_cache
def ordenes_compra(request):
    proveedores_catalogo = Proveedor.objects.filter(activo=True).prefetch_related('materiales').order_by('nombre')

    def _build_ordenes_recientes_data():
        ordenes = (
            OrdenCompra.objects
            .select_related('proveedor', 'creado_por')
            .prefetch_related('detalles')
            .order_by('-fecha_creacion')[:10]
        )

        data = []
        for orden in ordenes:
            detalles = list(orden.detalles.all())
            recibido_por_sku = {
                item['sku']: (item['total'] or Decimal('0'))
                for item in (
                    RecepcionMaterialDetalle.objects
                    .filter(
                        recepcion__orden_compra=orden.folio,
                        recepcion__proveedor_registrado=orden.proveedor,
                    )
                    .values('sku')
                    .annotate(total=Sum('cantidad_recibida'))
                )
            }

            total_pedido_cantidad = sum((detalle.cantidad_pedida for detalle in detalles), Decimal('0'))
            total_pedido_importe = sum((detalle.subtotal for detalle in detalles), Decimal('0'))
            total_recibido_cantidad = sum(recibido_por_sku.values(), Decimal('0'))
            avance = 0
            if total_pedido_cantidad > 0:
                avance = int(round((float(total_recibido_cantidad) / float(total_pedido_cantidad)) * 100))

            data.append({
                'id': orden.id,
                'folio': orden.folio,
                'proveedor': orden.proveedor.nombre,
                'fecha_orden': orden.fecha_orden,
                'fecha_prometida': orden.fecha_prometida,
                'estado': orden.estado,
                'estado_label': orden.get_estado_display(),
                'total_pedido_cantidad': total_pedido_cantidad,
                'total_recibido_cantidad': total_recibido_cantidad,
                'total_pedido_importe': total_pedido_importe,
                'avance': min(avance, 100),
                'lineas': len(detalles),
            })

        return data

    form_data = {
        'proveedor_id': '',
        'fecha_orden': date.today().isoformat(),
        'fecha_prometida': '',
        'condiciones_pago': '',
        'observaciones': '',
        'lineas': [
            {
                'sku': '',
                'cantidad_pedida': '',
                'precio_unitario': '',
            }
        ],
    }

    if request.method == 'POST' and (request.POST.get('accion_estado') or '').strip():
        orden_id = (request.POST.get('orden_id') or '').strip()
        accion_estado = (request.POST.get('accion_estado') or '').strip().upper()
        orden = OrdenCompra.objects.filter(id=orden_id).first()

        if not orden:
            messages.error(request, 'La orden seleccionada no existe.')
            return redirect('ordenes_compra')

        transiciones = _ordenes_transiciones_permitidas(orden.estado)
        if accion_estado not in transiciones:
            messages.error(request, f'No es posible cambiar de {orden.get_estado_display()} a {accion_estado}.')
            return redirect('ordenes_compra')

        orden.estado = accion_estado
        orden.save(update_fields=['estado'])
        messages.success(request, f'La orden {orden.folio} cambió a estado {orden.get_estado_display()}.')
        return redirect('ordenes_compra')

    if request.method == 'POST':
        proveedor_id = (request.POST.get('proveedor') or '').strip()
        fecha_orden_raw = (request.POST.get('fecha_orden') or '').strip()
        fecha_prometida_raw = (request.POST.get('fecha_prometida') or '').strip()
        condiciones_pago = (request.POST.get('condiciones_pago') or '').strip()
        observaciones = (request.POST.get('observaciones') or '').strip()
        accion = (request.POST.get('accion') or 'borrador').strip().lower()

        skus = request.POST.getlist('sku[]')
        cantidades = request.POST.getlist('cantidad_pedida[]')
        precios = request.POST.getlist('precio_unitario[]')

        form_data.update({
            'proveedor_id': proveedor_id,
            'fecha_orden': fecha_orden_raw,
            'fecha_prometida': fecha_prometida_raw,
            'condiciones_pago': condiciones_pago,
            'observaciones': observaciones,
            'lineas': [],
        })

        proveedor_obj = Proveedor.objects.filter(id=proveedor_id, activo=True).prefetch_related('materiales').first()
        fecha_orden = _parse_iso_date(fecha_orden_raw)
        fecha_prometida = _parse_iso_date(fecha_prometida_raw) if fecha_prometida_raw else None

        if not proveedor_obj:
            messages.error(request, 'Selecciona un proveedor válido.')
        elif not fecha_orden:
            messages.error(request, 'La fecha de la orden es obligatoria.')
        elif fecha_prometida and fecha_prometida < fecha_orden:
            messages.error(request, 'La fecha prometida no puede ser menor a la fecha de la orden.')
        elif condiciones_pago and condiciones_pago not in ORDEN_CONDICIONES_PAGO:
            messages.error(request, 'Selecciona una condición de pago válida del catálogo.')
        else:
            materiales_permitidos = {
                material.sku.upper(): material
                for material in proveedor_obj.materiales.filter(activo=True)
            }
            lineas = []
            total_rows = max(len(skus), len(cantidades), len(precios))

            for idx in range(total_rows):
                sku = (skus[idx] if idx < len(skus) else '').strip().upper()
                cantidad_texto = (cantidades[idx] if idx < len(cantidades) else '').strip()
                precio_texto = (precios[idx] if idx < len(precios) else '').strip()

                row_has_data = any([sku, cantidad_texto, precio_texto])
                if not row_has_data:
                    continue

                form_data['lineas'].append({
                    'sku': sku,
                    'cantidad_pedida': cantidad_texto,
                    'precio_unitario': precio_texto,
                })

                if not sku or not cantidad_texto:
                    messages.error(request, f'En la fila {idx + 1}, SKU y cantidad son obligatorios.')
                    lineas = []
                    break

                material_obj = materiales_permitidos.get(sku)
                if not material_obj:
                    messages.error(request, f'En la fila {idx + 1}, el material no pertenece al proveedor seleccionado.')
                    lineas = []
                    break

                try:
                    cantidad_pedida = Decimal(cantidad_texto.replace(',', '.'))
                    precio_unitario = Decimal((precio_texto or '0').replace(',', '.'))
                except InvalidOperation:
                    messages.error(request, f'En la fila {idx + 1}, cantidad y precio deben ser numéricos válidos.')
                    lineas = []
                    break

                if cantidad_pedida <= 0:
                    messages.error(request, f'En la fila {idx + 1}, la cantidad debe ser mayor a cero.')
                    lineas = []
                    break

                if precio_unitario < 0:
                    messages.error(request, f'En la fila {idx + 1}, el precio unitario no puede ser negativo.')
                    lineas = []
                    break

                subtotal = (cantidad_pedida * precio_unitario).quantize(Decimal('0.01'))
                lineas.append({
                    'material': material_obj,
                    'sku': material_obj.sku,
                    'descripcion': material_obj.nombre,
                    'um': material_obj.um,
                    'cantidad_pedida': cantidad_pedida,
                    'precio_unitario': precio_unitario,
                    'subtotal': subtotal,
                })

            if not form_data['lineas']:
                form_data['lineas'] = [{'sku': '', 'cantidad_pedida': '', 'precio_unitario': ''}]

            if lineas:
                estado_destino = {
                    'borrador': OrdenCompra.EstadoOrden.BORRADOR,
                    'aprobar': OrdenCompra.EstadoOrden.APROBADA,
                    'enviar': OrdenCompra.EstadoOrden.ENVIADA,
                }.get(accion, OrdenCompra.EstadoOrden.BORRADOR)

                total_estimado = sum((linea['subtotal'] for linea in lineas), Decimal('0'))

                with transaction.atomic():
                    folio = _next_orden_compra_folio()
                    while OrdenCompra.objects.filter(folio=folio).exists():
                        folio = _next_orden_compra_folio()

                    orden = OrdenCompra.objects.create(
                        folio=folio,
                        proveedor=proveedor_obj,
                        fecha_orden=fecha_orden,
                        fecha_prometida=fecha_prometida,
                        condiciones_pago=condiciones_pago,
                        observaciones=observaciones,
                        estado=estado_destino,
                        total_estimado=total_estimado,
                        creado_por=request.user,
                    )

                    for linea in lineas:
                        OrdenCompraDetalle.objects.create(
                            orden=orden,
                            material=linea['material'],
                            sku=linea['sku'],
                            descripcion=linea['descripcion'],
                            um=linea['um'],
                            cantidad_pedida=linea['cantidad_pedida'],
                            precio_unitario=linea['precio_unitario'],
                            subtotal=linea['subtotal'],
                        )

                        ProveedorMaterialPrecio.objects.update_or_create(
                            proveedor=proveedor_obj,
                            material=linea['material'],
                            defaults={
                                'precio_unitario': linea['precio_unitario'],
                            },
                        )

                messages.success(request, f'Orden de compra {orden.folio} creada en estado {orden.get_estado_display()}.')
                return redirect('ordenes_compra')

    return render(
        request,
        'inventario/ordenes_compra.html',
        {
            'proveedores_catalogo': proveedores_catalogo,
            'ordenes_recientes': _build_ordenes_recientes_data(),
            'opciones_condiciones_pago': ORDEN_CONDICIONES_PAGO,
            'form_data': form_data,
            'estado_enviada': OrdenCompra.EstadoOrden.ENVIADA,
            'estado_parcial': OrdenCompra.EstadoOrden.PARCIAL,
            'estado_borrador': OrdenCompra.EstadoOrden.BORRADOR,
            'estado_aprobada': OrdenCompra.EstadoOrden.APROBADA,
        },
    )


@login_required(login_url='login')
@never_cache
def bom_lista_materiales(request):
    material_sku = (request.GET.get('material_sku') or '').strip().upper()
    material_consultado = None
    materiales_catalogo = list(Material.objects.filter(activo=True).order_by('sku'))
    boms_registrados = (
        BOM.objects
        .filter(tipo=BOM.TipoBOM.MATERIALES)
        .select_related('creado_por')
        .prefetch_related('componentes__material')
        .order_by('producto', 'version')
    )

    if material_sku:
        material_consultado = Material.objects.filter(sku__iexact=material_sku, activo=True).first()
        if material_consultado:
            boms_registrados = boms_registrados.filter(componentes__material=material_consultado).distinct()
        else:
            boms_registrados = boms_registrados.none()

    form_data = {
        'codigo': '',
        'producto': '',
        'version': '1.0',
        'descripcion': '',
        'cantidad_base': '1',
        'unidad_producto': '',
        'activo': True,
        'componentes': [
            {
                'material_id': '',
                'cantidad': '',
                'observaciones': '',
            }
        ],
    }

    if request.method == 'POST':
        codigo = (request.POST.get('codigo') or '').strip().upper()
        producto = (request.POST.get('producto') or '').strip()
        version = (request.POST.get('version') or '1.0').strip()
        descripcion = (request.POST.get('descripcion') or '').strip()
        cantidad_base_texto = (request.POST.get('cantidad_base') or '1').strip()
        unidad_producto = (request.POST.get('unidad_producto') or '').strip()
        activo = (request.POST.get('activo') or '1').strip() == '1'

        materiales_ids = request.POST.getlist('material_id[]')
        cantidades = request.POST.getlist('cantidad[]')
        observaciones_list = request.POST.getlist('observaciones[]')

        form_data.update({
            'codigo': codigo,
            'producto': producto,
            'version': version,
            'descripcion': descripcion,
            'cantidad_base': cantidad_base_texto,
            'unidad_producto': unidad_producto,
            'activo': activo,
            'componentes': [],
        })

        try:
            cantidad_base = Decimal(cantidad_base_texto.replace(',', '.'))
        except InvalidOperation:
            cantidad_base = None

        if not codigo:
            messages.error(request, 'El código BOM es obligatorio.')
        elif not producto:
            messages.error(request, 'El nombre del producto es obligatorio.')
        elif not version:
            messages.error(request, 'La versión del BOM es obligatoria.')
        elif cantidad_base is None:
            messages.error(request, 'La cantidad base debe ser numérica válida.')
        elif cantidad_base <= 0:
            messages.error(request, 'La cantidad base debe ser mayor a cero.')
        elif BOM.objects.filter(codigo__iexact=codigo, version__iexact=version).exists():
            messages.error(request, 'Ya existe un BOM con ese código y versión.')
        else:
            materiales_validos = {str(material.id): material for material in materiales_catalogo}
            componentes = []
            materiales_usados = set()
            total_rows = max(len(materiales_ids), len(cantidades), len(observaciones_list))

            for idx in range(total_rows):
                material_id = (materiales_ids[idx] if idx < len(materiales_ids) else '').strip()
                cantidad_texto = (cantidades[idx] if idx < len(cantidades) else '').strip()
                observaciones = (observaciones_list[idx] if idx < len(observaciones_list) else '').strip()

                row_has_data = any([material_id, cantidad_texto, observaciones])
                if not row_has_data:
                    continue

                form_data['componentes'].append({
                    'material_id': material_id,
                    'cantidad': cantidad_texto,
                    'observaciones': observaciones,
                })

                material_obj = materiales_validos.get(material_id)
                if not material_obj:
                    messages.error(request, f'En la fila {idx + 1}, selecciona un material válido.')
                    componentes = []
                    break

                if material_id in materiales_usados:
                    messages.error(request, f'En la fila {idx + 1}, el material {material_obj.sku} está duplicado.')
                    componentes = []
                    break

                try:
                    cantidad = Decimal(cantidad_texto.replace(',', '.'))
                except InvalidOperation:
                    messages.error(request, f'En la fila {idx + 1}, la cantidad requerida debe ser numérica válida.')
                    componentes = []
                    break

                if cantidad <= 0:
                    messages.error(request, f'En la fila {idx + 1}, la cantidad requerida debe ser mayor a cero.')
                    componentes = []
                    break

                materiales_usados.add(material_id)
                componentes.append({
                    'material': material_obj,
                    'cantidad': cantidad,
                    'observaciones': observaciones,
                })

            if not form_data['componentes']:
                form_data['componentes'] = [{'material_id': '', 'cantidad': '', 'observaciones': ''}]

            if componentes:
                with transaction.atomic():
                    bom = BOM.objects.create(
                        codigo=codigo,
                        producto=producto,
                        version=version,
                        descripcion=descripcion,
                        cantidad_base=cantidad_base,
                        unidad_producto=unidad_producto,
                        activo=activo,
                        creado_por=request.user,
                    )

                    BOMDetalle.objects.bulk_create([
                        BOMDetalle(
                            bom=bom,
                            material=componente['material'],
                            cantidad=componente['cantidad'],
                            observaciones=componente['observaciones'],
                        )
                        for componente in componentes
                    ])

                messages.success(request, f'BOM {bom.codigo} v{bom.version} creado correctamente.')
                return redirect('bom_lista_materiales')

    bom_resumenes = []
    total_componentes = 0
    for bom in boms_registrados:
        componentes = list(bom.componentes.all())
        componentes_count = len(componentes)
        total_componentes += componentes_count
        bom_resumenes.append({
            'id': bom.id,
            'codigo': bom.codigo,
            'producto': bom.producto,
            'version': bom.version,
            'descripcion': bom.descripcion,
            'cantidad_base': bom.cantidad_base,
            'unidad_producto': bom.unidad_producto,
            'activo': bom.activo,
            'creado_por': bom.creado_por,
            'fecha_creacion': bom.fecha_creacion,
            'componentes_count': componentes_count,
            'materiales': componentes,
        })

    return render(
        request,
        'inventario/bom.html',
        {
            'materiales_catalogo': materiales_catalogo,
            'boms_registrados': bom_resumenes,
            'form_data': form_data,
            'material_consultado': material_consultado,
            'material_sku_filtrado': material_sku,
            'bom_kpis': {
                'total_boms': len(bom_resumenes),
                'boms_activos': sum(1 for bom in bom_resumenes if bom['activo']),
                'total_materiales_catalogo': len(materiales_catalogo),
                'total_componentes': total_componentes,
            },
        },
    )


@login_required(login_url='login')
@never_cache
def proveedores_alta(request):
    materiales_catalogo = Material.objects.filter(activo=True).order_by('sku')
    proveedores_recientes = Proveedor.objects.prefetch_related('materiales').order_by('nombre')

    form_data = {
        'nombre': '',
        'descripcion': '',
        'telefono': '',
        'email': '',
        'activo': True,
        'materiales_ids': [],
        'precios_materiales': {},
    }

    if request.method == 'POST':
        nombre = (request.POST.get('nombre') or '').strip()
        descripcion = (request.POST.get('descripcion') or '').strip()
        telefono = (request.POST.get('telefono') or '').strip()
        email = (request.POST.get('email') or '').strip()
        activo = (request.POST.get('activo') or '1').strip() == '1'
        materiales_ids = [item for item in request.POST.getlist('materiales[]') if item]
        precios_materiales_raw = request.POST.getlist('precio_material[]')

        precios_materiales_map = {}
        for idx, material_id in enumerate(materiales_ids):
            precio_texto = (precios_materiales_raw[idx] if idx < len(precios_materiales_raw) else '').strip()
            precios_materiales_map[str(material_id)] = precio_texto or '0'

        form_data.update({
            'nombre': nombre,
            'descripcion': descripcion,
            'telefono': telefono,
            'email': email,
            'activo': activo,
            'materiales_ids': materiales_ids,
            'precios_materiales': precios_materiales_map,
        })

        if not nombre:
            messages.error(request, 'El nombre del proveedor es obligatorio.')
        elif Proveedor.objects.filter(nombre__iexact=nombre).exists():
            messages.error(request, 'Ya existe un proveedor con ese nombre.')
        else:
            precios_materiales_decimal = {}
            for material_id, precio_texto in precios_materiales_map.items():
                try:
                    precio_unitario = Decimal((precio_texto or '0').replace(',', '.'))
                except InvalidOperation:
                    messages.error(request, 'El precio unitario de materiales debe ser numérico válido.')
                    return render(
                        request,
                        'inventario/proveedores.html',
                        {
                            'materiales_catalogo': materiales_catalogo,
                            'proveedores_recientes': proveedores_recientes,
                            'form_data': form_data,
                        },
                    )

                if precio_unitario < 0:
                    messages.error(request, 'El precio unitario de materiales no puede ser negativo.')
                    return render(
                        request,
                        'inventario/proveedores.html',
                        {
                            'materiales_catalogo': materiales_catalogo,
                            'proveedores_recientes': proveedores_recientes,
                            'form_data': form_data,
                        },
                    )

                precios_materiales_decimal[material_id] = precio_unitario

            with transaction.atomic():
                proveedor = Proveedor.objects.create(
                    nombre=nombre,
                    descripcion=descripcion,
                    telefono=telefono,
                    email=email,
                    activo=activo,
                )

                materiales_validos = Material.objects.filter(id__in=materiales_ids, activo=True)
                if materiales_validos.exists():
                    proveedor.materiales.set(materiales_validos)

                    precios_bulk = []
                    for material in materiales_validos:
                        precio = precios_materiales_decimal.get(str(material.id), Decimal('0'))
                        precios_bulk.append(
                            ProveedorMaterialPrecio(
                                proveedor=proveedor,
                                material=material,
                                precio_unitario=precio,
                            )
                        )

                    if precios_bulk:
                        ProveedorMaterialPrecio.objects.bulk_create(precios_bulk)

            messages.success(request, f'Proveedor {proveedor.nombre} creado correctamente.')
            return redirect('proveedores_alta')

    return render(
        request,
        'inventario/proveedores.html',
        {
            'materiales_catalogo': materiales_catalogo,
            'proveedores_recientes': proveedores_recientes,
            'form_data': form_data,
        },
    )


@login_required(login_url='login')
def inventario_almacen(request):
    _ensure_almacenes_base()

    decimal_output = DecimalField(max_digits=14, decimal_places=2)
    fecha_inicio_raw = (request.GET.get('fecha_inicio') or '').strip()
    fecha_fin_raw = (request.GET.get('fecha_fin') or '').strip()
    fecha_historial_raw = (request.GET.get('fecha_historial') or '').strip()
    fecha_inicio = _parse_iso_date(fecha_inicio_raw)
    fecha_fin = _parse_iso_date(fecha_fin_raw)
    fecha_historial = _parse_iso_date(fecha_historial_raw)

    if fecha_inicio and fecha_fin and fecha_inicio > fecha_fin:
        fecha_inicio, fecha_fin = fecha_fin, fecha_inicio
        fecha_inicio_raw = fecha_inicio.isoformat()
        fecha_fin_raw = fecha_fin.isoformat()

    almacenes = Almacen.objects.filter(activo=True).order_by('codigo').prefetch_related(
        'inventarios_material__material'
    )

    movimientos_filtrados = RecepcionMaterialDetalle.objects.all()

    movimientos_posteriores = RecepcionMaterialDetalle.objects.none()
    historico_almacenes = {}
    if fecha_historial:
        movimientos_posteriores = RecepcionMaterialDetalle.objects.filter(
            recepcion__fecha_recepcion__gt=fecha_historial,
            ubicacion_destino__isnull=False,
            estatus=RecepcionMaterialDetalle.EstatusDetalle.ACEPTADO,
        ).values('ubicacion_destino', 'material_id').annotate(total_recibido=Sum('cantidad_recibida'))

        recibidos_map = {
            (item['ubicacion_destino'], item['material_id']): item['total_recibido']
            for item in movimientos_posteriores
        }

        historico_almacenes = {
            'recibidos': recibidos_map,
            'salidas': {},
        }

    if fecha_inicio:
        movimientos_filtrados = movimientos_filtrados.filter(recepcion__fecha_recepcion__gte=fecha_inicio)

    if fecha_fin:
        movimientos_filtrados = movimientos_filtrados.filter(recepcion__fecha_recepcion__lte=fecha_fin)

    movimientos_por_almacen = {
        item['ubicacion_destino']: item
        for item in movimientos_filtrados.exclude(ubicacion_destino='').values('ubicacion_destino').annotate(
            materiales_ok=Count('id', filter=Q(estatus=RecepcionMaterialDetalle.EstatusDetalle.ACEPTADO)),
            materiales_rechazados=Count(
                'id',
                filter=Q(estatus__in=[
                    RecepcionMaterialDetalle.EstatusDetalle.RECHAZADO,
                    RecepcionMaterialDetalle.EstatusDetalle.DIFERENCIA,
                ]),
            ),
            cantidad_ok=Sum(
                Case(
                    When(estatus=RecepcionMaterialDetalle.EstatusDetalle.ACEPTADO, then='cantidad_recibida'),
                    default=Value(Decimal('0.00')),
                    output_field=decimal_output,
                )
            ),
            cantidad_rechazada=Sum(
                Case(
                    When(
                        estatus__in=[
                            RecepcionMaterialDetalle.EstatusDetalle.RECHAZADO,
                            RecepcionMaterialDetalle.EstatusDetalle.DIFERENCIA,
                        ],
                        then='cantidad_recibida',
                    ),
                    default=Value(Decimal('0.00')),
                    output_field=decimal_output,
                )
            ),
        )
    }

    linea_por_almacen = {}

    timeline_queryset = movimientos_filtrados.values('recepcion__fecha_recepcion').annotate(
        material_ok=Sum(
            Case(
                When(estatus=RecepcionMaterialDetalle.EstatusDetalle.ACEPTADO, then='cantidad_recibida'),
                default=Value(Decimal('0.00')),
                output_field=decimal_output,
            )
        ),
        material_malo=Sum(
            Case(
                When(
                    estatus__in=[
                        RecepcionMaterialDetalle.EstatusDetalle.RECHAZADO,
                        RecepcionMaterialDetalle.EstatusDetalle.DIFERENCIA,
                    ],
                    then='cantidad_recibida',
                ),
                default=Value(Decimal('0.00')),
                output_field=decimal_output,
            )
        ),
    ).order_by('recepcion__fecha_recepcion')

    timeline_almacen_queryset = movimientos_filtrados.exclude(ubicacion_destino='').values(
        'recepcion__fecha_recepcion',
        'ubicacion_destino',
    ).annotate(
        material_ok=Sum(
            Case(
                When(estatus=RecepcionMaterialDetalle.EstatusDetalle.ACEPTADO, then='cantidad_recibida'),
                default=Value(Decimal('0.00')),
                output_field=decimal_output,
            )
        ),
        material_malo=Sum(
            Case(
                When(
                    estatus__in=[
                        RecepcionMaterialDetalle.EstatusDetalle.RECHAZADO,
                        RecepcionMaterialDetalle.EstatusDetalle.DIFERENCIA,
                    ],
                    then='cantidad_recibida',
                ),
                default=Value(Decimal('0.00')),
                output_field=decimal_output,
            )
        ),
    ).order_by('recepcion__fecha_recepcion', 'ubicacion_destino')

    timeline_almacenes_por_fecha = defaultdict(list)
    for item in timeline_almacen_queryset:
        fecha = item['recepcion__fecha_recepcion']
        if not fecha:
            continue

        timeline_almacenes_por_fecha[fecha.strftime('%Y-%m-%d')].append({
            'codigo': item['ubicacion_destino'],
            'material_ok': _decimal_to_float(item['material_ok']),
            'material_malo': _decimal_to_float(item['material_malo']),
            'mandado_linea': 0.0,
        })
    timeline_linea_map = {}

    timeline_map = {
        item['recepcion__fecha_recepcion'].strftime('%Y-%m-%d'): {
            'fecha': item['recepcion__fecha_recepcion'].strftime('%Y-%m-%d'),
            'material_ok': _decimal_to_float(item['material_ok']),
            'material_malo': _decimal_to_float(item['material_malo']),
            'mandado_linea': timeline_linea_map.get(item['recepcion__fecha_recepcion'].strftime('%Y-%m-%d'), 0.0),
            'almacenes': timeline_almacenes_por_fecha.get(item['recepcion__fecha_recepcion'].strftime('%Y-%m-%d'), []),
        }
        for item in timeline_queryset
        if item['recepcion__fecha_recepcion']
    }

    timeline_data = []
    if fecha_inicio and fecha_fin:
        current_date = fecha_inicio
        while current_date <= fecha_fin:
            current_key = current_date.isoformat()
            timeline_data.append(
                timeline_map.get(
                    current_key,
                    {
                        'fecha': current_key,
                        'material_ok': 0.0,
                        'material_malo': 0.0,
                        'mandado_linea': timeline_linea_map.get(current_key, 0.0),
                        'almacenes': timeline_almacenes_por_fecha.get(current_key, []),
                    },
                )
            )
            current_date += timedelta(days=1)
    else:
        fechas_timeline = sorted(set(timeline_map.keys()) | set(timeline_linea_map.keys()))
        for current_key in fechas_timeline:
            timeline_data.append(
                timeline_map.get(
                    current_key,
                    {
                        'fecha': current_key,
                        'material_ok': 0.0,
                        'material_malo': 0.0,
                        'mandado_linea': timeline_linea_map.get(current_key, 0.0),
                        'almacenes': timeline_almacenes_por_fecha.get(current_key, []),
                    },
                )
            )

    resumen_movimientos = movimientos_filtrados.aggregate(
        materiales_ok=Count('id', filter=Q(estatus=RecepcionMaterialDetalle.EstatusDetalle.ACEPTADO)),
        materiales_rechazados=Count(
            'id',
            filter=Q(estatus__in=[
                RecepcionMaterialDetalle.EstatusDetalle.RECHAZADO,
                RecepcionMaterialDetalle.EstatusDetalle.DIFERENCIA,
            ]),
        ),
    )

    resumen_linea = {
        'movimientos_linea': 0,
        'cantidad_linea': Decimal('0'),
    }

    resumen_inspeccion = movimientos_filtrados.aggregate(
        material_ok=Count('id', filter=Q(estatus=RecepcionMaterialDetalle.EstatusDetalle.ACEPTADO)),
        material_rechazado=Count('id', filter=Q(estatus__in=[
            RecepcionMaterialDetalle.EstatusDetalle.RECHAZADO,
            RecepcionMaterialDetalle.EstatusDetalle.DIFERENCIA,
        ])),
        pzas_ok=Sum(
            Case(
                When(estatus=RecepcionMaterialDetalle.EstatusDetalle.ACEPTADO, then='cantidad_recibida'),
                default=Value(Decimal('0.00')),
                output_field=decimal_output,
            )
        ),
        pzas_rechazadas=Sum(
            Case(
                When(
                    estatus__in=[
                        RecepcionMaterialDetalle.EstatusDetalle.RECHAZADO,
                        RecepcionMaterialDetalle.EstatusDetalle.DIFERENCIA,
                    ],
                    then='cantidad_recibida',
                ),
                default=Value(Decimal('0.00')),
                output_field=decimal_output,
            )
        ),
    )

    almacenes_data = []
    max_stock = Decimal('0')

    for almacen in almacenes:
        inventarios = [
            inventario for inventario in almacen.inventarios_material.all()
            if inventario.stock_actual and inventario.stock_actual > 0
        ]
        inventarios.sort(key=lambda item: item.material.sku)

        total_stock = sum((inventario.stock_actual for inventario in inventarios), Decimal('0'))
        total_materiales = len(inventarios)
        lotes_distintos = len({inventario.lote or 'Sin lote registrado' for inventario in inventarios})
        max_stock = max(max_stock, total_stock)
        resumen_almacen = movimientos_por_almacen.get(almacen.codigo, {})

        materiales_ok = resumen_almacen.get('materiales_ok') or 0
        materiales_rechazados = resumen_almacen.get('materiales_rechazados') or 0
        cantidad_ok = resumen_almacen.get('cantidad_ok') or Decimal('0')
        cantidad_rechazada = resumen_almacen.get('cantidad_rechazada') or Decimal('0')

        pzas_inspeccionadas_almacen = (cantidad_ok or Decimal('0')) + (cantidad_rechazada or Decimal('0'))
        almacenes_data.append({
            'almacen': almacen,
            'inventarios': inventarios,
            'total_stock': total_stock,
            'total_materiales': total_materiales,
            'lotes_distintos': lotes_distintos,
            'materiales_ok': materiales_ok,
            'materiales_rechazados': materiales_rechazados,
            'arribos_inspeccionados': materiales_ok + materiales_rechazados,
            'cantidad_ok': cantidad_ok,
            'cantidad_rechazada': cantidad_rechazada,
            'pzas_inspeccionadas': pzas_inspeccionadas_almacen,
            'mandado_linea': (linea_por_almacen.get(almacen.codigo, {}) or {}).get('cantidad_linea') or Decimal('0'),
        })

    for bloque in almacenes_data:
        if max_stock > 0:
            bloque['participacion_stock'] = round((bloque['total_stock'] / max_stock) * 100, 1)
        else:
            bloque['participacion_stock'] = 0

    if fecha_historial:
        for bloque in almacenes_data:
            historico_inventario = []
            historico_total_stock = Decimal('0')
            for inventario in bloque['inventarios']:
                key = (bloque['almacen'].codigo, inventario.material_id)
                recibidos = historico_almacenes['recibidos'].get(key, Decimal('0'))
                salidas = historico_almacenes['salidas'].get(key, Decimal('0'))
                stock_historico = (inventario.stock_actual or Decimal('0')) - (recibidos or Decimal('0')) + (salidas or Decimal('0'))
                if stock_historico < 0:
                    stock_historico = Decimal('0')
                historico_total_stock += stock_historico
                historico_inventario.append({
                    'sku': inventario.material.sku,
                    'nombre': inventario.material.nombre,
                    'lote': inventario.lote or 'Sin lote registrado',
                    'um': inventario.material.um or '-',
                    'stock_historico': stock_historico,
                })
            bloque['historico_total_stock'] = historico_total_stock
            bloque['historico_inventario'] = historico_inventario

    stock_por_almacen = sorted(
        [
            {
                'codigo': bloque['almacen'].codigo,
                'nombre': bloque['almacen'].nombre,
                'stock': bloque['total_stock'],
                'materiales_ok': bloque['materiales_ok'],
                'materiales_rechazados': bloque['materiales_rechazados'],
                'mandado_linea': bloque['mandado_linea'],
            }
            for bloque in almacenes_data
        ],
        key=lambda item: item['stock'],
        reverse=True,
    )[:6]

    max_stock_almacenes = stock_por_almacen[0]['stock'] if stock_por_almacen else Decimal('0')
    for item in stock_por_almacen:
        item['percent'] = int(round((item['stock'] / max_stock_almacenes) * 100)) if max_stock_almacenes else 0

    top_materiales_stock = list(
        InventarioAlmacen.objects.filter(
            almacen__activo=True,
            material__activo=True,
            stock_actual__gt=0,
        ).values('material__sku', 'material__nombre').annotate(
            stock_total=Sum('stock_actual'),
        ).order_by('-stock_total')[:6]
    )

    max_material_stock = top_materiales_stock[0]['stock_total'] if top_materiales_stock else Decimal('0')
    for item in top_materiales_stock:
        item['percent'] = int(round((item['stock_total'] / max_material_stock) * 100)) if max_material_stock else 0

    movimientos_almacenes = [
        {
            'codigo': bloque['almacen'].codigo,
            'nombre': bloque['almacen'].nombre,
            'materiales_ok': bloque['materiales_ok'],
            'materiales_rechazados': bloque['materiales_rechazados'],
            'mandado_linea': bloque['mandado_linea'],
        }
        for bloque in almacenes_data
    ]

    resumen_global = InventarioAlmacen.objects.filter(
        almacen__activo=True,
        stock_actual__gt=0,
    ).aggregate(total_stock=Sum('stock_actual'))

    context = {
        'almacenes_data': almacenes_data,
        'stock_por_almacen': stock_por_almacen,
        'top_materiales_stock': top_materiales_stock,
        'movimientos_almacenes': movimientos_almacenes,
        'total_almacenes': len(almacenes_data),
        'total_stock_global': resumen_global.get('total_stock') or Decimal('0'),
        'materiales_ok_global': resumen_movimientos.get('materiales_ok') or 0,
        'materiales_rechazados_global': resumen_movimientos.get('materiales_rechazados') or 0,
        'materiales_linea_global': resumen_linea.get('cantidad_linea') or Decimal('0'),
        'arribos_inspeccionados_global': (resumen_inspeccion.get('material_ok') or 0) + (resumen_inspeccion.get('material_rechazado') or 0),
        'pzas_inspeccionadas_global': (resumen_inspeccion.get('pzas_ok') or Decimal('0')) + (resumen_inspeccion.get('pzas_rechazadas') or Decimal('0')),
        'timeline_data': timeline_data,
        'fecha_inicio_value': fecha_inicio_raw,
        'fecha_fin_value': fecha_fin_raw,
        'fecha_historial_value': fecha_historial_raw,
        'fecha_historial': fecha_historial,
        'historico_almacenes': historico_almacenes,
    }
    return render(request, 'inventario/almacen.html', context)


@login_required(login_url='login')
def historial_recepciones(request):
    recepciones = RecepcionMaterial.objects.select_related('creado_por').prefetch_related('detalles')
    return render(request, 'inventario/historial_recepciones.html', {'recepciones': recepciones})


@login_required(login_url='login')
def historial_almacen(request):
    almacen_codigo = (request.GET.get('almacen') or '').strip().upper()
    fecha_movimiento_raw = (request.GET.get('fecha') or '').strip()
    tipo_movimiento = (request.GET.get('tipo') or '').strip().upper()
    fecha_movimiento = _parse_iso_date(fecha_movimiento_raw)

    if tipo_movimiento not in {'', 'ENTRADA', 'SALIDA'}:
        tipo_movimiento = ''

    almacenes_catalogo = Almacen.objects.filter(activo=True).order_by('codigo')

    movimientos = []

    entradas_qs = (
        RecepcionMaterialDetalle.objects
        .select_related('recepcion', 'recepcion__creado_por', 'material')
        .exclude(ubicacion_destino='')
    )
    if almacen_codigo:
        entradas_qs = entradas_qs.filter(ubicacion_destino=almacen_codigo)
    if fecha_movimiento:
        entradas_qs = entradas_qs.filter(recepcion__fecha_recepcion=fecha_movimiento)

    if tipo_movimiento in {'', 'ENTRADA'}:
        for detalle in entradas_qs:
            movimientos.append({
                'fecha': detalle.recepcion.fecha_recepcion,
                'tipo': 'ENTRADA',
                'almacen': detalle.ubicacion_destino,
                'sku': detalle.sku,
                'material': detalle.descripcion,
                'lote': detalle.lote or '-',
                'cantidad': detalle.cantidad_recibida,
                'referencia': f"REC-{detalle.recepcion_id}",
                'usuario': detalle.recepcion.creado_por.get_full_name() or detalle.recepcion.creado_por.username,
            })

    salidas_qs = (
        SalidaLineaDetalle.objects
        .select_related('salida', 'salida__creado_por', 'almacen_origen', 'material')
    )
    if almacen_codigo:
        salidas_qs = salidas_qs.filter(almacen_origen__codigo=almacen_codigo)
    if fecha_movimiento:
        salidas_qs = salidas_qs.filter(salida__fecha_salida=fecha_movimiento)

    if tipo_movimiento in {'', 'SALIDA'}:
        for detalle in salidas_qs:
            movimientos.append({
                'fecha': detalle.salida.fecha_salida,
                'tipo': 'SALIDA',
                'almacen': detalle.almacen_origen.codigo if detalle.almacen_origen else '-',
                'sku': detalle.sku,
                'material': detalle.descripcion,
                'lote': detalle.lote or '-',
                'cantidad': detalle.cantidad_enviada,
                'referencia': f"SAL-{detalle.salida_id}",
                'usuario': detalle.salida.creado_por.get_full_name() or detalle.salida.creado_por.username,
            })

    movimientos.sort(key=lambda item: (item['fecha'], item['referencia']), reverse=True)

    return render(
        request,
        'inventario/historial_almacen.html',
        {
            'almacenes_catalogo': almacenes_catalogo,
            'almacen_seleccionado': almacen_codigo,
            'fecha_seleccionada': fecha_movimiento_raw,
            'tipo_seleccionado': tipo_movimiento,
            'movimientos': movimientos,
        },
    )


@login_required(login_url='login')
@never_cache
def qa_sqa(request):
    if request.method == 'POST':
        accion = (request.POST.get('accion') or '').strip()
        lote = (request.POST.get('lote') or '').strip()

        if accion == 'liberar':
            messages.success(request, f'Lote {lote or "seleccionado"} aprobado por SQA y liberado para producción (demo).')
        elif accion == 'rechazar':
            messages.error(request, f'Lote {lote or "seleccionado"} rechazado por SQA y enviado a cuarentena (demo).')
        else:
            messages.success(request, 'Inspección SQA guardada (demo).')

        return redirect('qa_sqa')

    return render(request, 'qa/sqa.html')


@login_required(login_url='login')
@never_cache
def qa_oqa(request):
    if request.method == 'POST':
        lote = (request.POST.get('lote_producto') or '').strip()
        decision = (request.POST.get('decision_final') or '').strip()

        if decision == 'liberado':
            messages.success(request, f'Lote terminado {lote or "seleccionado"} liberado por OQA para embarque (demo).')
        elif decision == 'retenido':
            messages.error(request, f'Lote terminado {lote or "seleccionado"} retenido por OQA para retrabajo (demo).')
        else:
            messages.success(request, 'Evaluación OQA guardada (demo).')

        return redirect('qa_oqa')

    return render(request, 'qa/oqa.html')


@login_required(login_url='login')
@never_cache
def qa_customer_service(request):
    if request.method == 'POST':
        folio = (request.POST.get('folio_reclamo') or '').strip()
        estado = (request.POST.get('estado_reclamo') or '').strip()

        if estado == 'cerrado':
            messages.success(request, f'Reclamo {folio or "seleccionado"} cerrado y comunicado al cliente (demo).')
        else:
            messages.success(request, f'Reclamo {folio or "seleccionado"} actualizado en Customer Service (demo).')

        return redirect('qa_customer_service')

    return render(request, 'qa/customer_service.html')


@login_required(login_url='login')
def calidad_inspeccion_material(request):
    return redirect('qa_sqa')


@login_required(login_url='login')
def api_materiales_proveedor(request, proveedor_id):
    """
    API que devuelve los materiales asociados a un proveedor específico en formato JSON.
    """

    try:
        proveedor = Proveedor.objects.get(id=proveedor_id, activo=True)
    except Proveedor.DoesNotExist:
        return JsonResponse({'error': 'Proveedor no encontrado'}, status=404)
    
    precios_map = {
        item['material_id']: item['precio_unitario']
        for item in (
            ProveedorMaterialPrecio.objects
            .filter(proveedor=proveedor)
            .values('material_id', 'precio_unitario')
        )
    }

    ultimos_precios_oc = {
        item['material_id']: item['precio_unitario']
        for item in (
            OrdenCompraDetalle.objects
            .filter(
                orden__proveedor=proveedor,
                precio_unitario__gt=0,
            )
            .order_by('-id')
            .values('material_id', 'precio_unitario')
        )
        if item['material_id'] not in precios_map
    }

    # Obtener los materiales del proveedor
    materiales = proveedor.materiales.filter(activo=True).values('id', 'sku', 'nombre', 'descripcion', 'um')
    
    # Construir el JSON en el formato esperado por el frontend
    materiales_dict = {
        m['sku']: {
            'sku': m['sku'],
            'nombre': m['nombre'],
            'descripcion': m['descripcion'],
            'um': m['um'],
            'precio_unitario': float(precios_map.get(m['id']) or ultimos_precios_oc.get(m['id']) or 0),
        }
        for m in materiales
    }
    
    return JsonResponse(materiales_dict)


# ─── PRODUCCIÓN ──────────────────────────────────────────────────────────────

@login_required(login_url='login')
@never_cache
def bom_mfg(request):
    materiales_catalogo = list(Material.objects.filter(activo=True).order_by('sku'))
    boms_mfg = (
        BOM.objects
        .filter(tipo=BOM.TipoBOM.MFG)
        .select_related('creado_por')
        .prefetch_related('componentes__material', 'operaciones')
        .order_by('producto', 'version')
    )

    bom_kpis = {
        'total': boms_mfg.count(),
        'activos': boms_mfg.filter(activo=True).count(),
        'total_materiales': Material.objects.filter(activo=True).count(),
    }

    LINEAS = ['Línea SMT-01', 'Línea SMT-02', 'Línea SMT-03']
    UNIDADES_TIEMPO = [('min', 'Minutos'), ('hrs', 'Horas'), ('seg', 'Segundos')]

    form_data = {
        'codigo': '', 'producto': '', 'version': '1.0',
        'descripcion': '', 'cantidad_base': '1', 'unidad_producto': '', 'activo': True,
        'componentes': [{'material_id': '', 'cantidad': '', 'observaciones': ''}],
        'operaciones': [{'secuencia': '1', 'nombre': '', 'descripcion': '', 'linea_produccion': '',
                         'tiempo_estimado': '', 'unidad_tiempo': 'min', 'recurso_maquina': '', 'operadores_requeridos': '1'}],
    }

    if request.method == 'POST':
        codigo = (request.POST.get('codigo') or '').strip().upper()
        producto = (request.POST.get('producto') or '').strip()
        version = (request.POST.get('version') or '1.0').strip()
        descripcion = (request.POST.get('descripcion') or '').strip()
        cantidad_base_texto = (request.POST.get('cantidad_base') or '1').strip()
        unidad_producto = (request.POST.get('unidad_producto') or '').strip()
        activo = (request.POST.get('activo') or '1').strip() == '1'

        materiales_ids = request.POST.getlist('material_id[]')
        cantidades = request.POST.getlist('cantidad[]')
        observaciones_list = request.POST.getlist('observaciones[]')

        op_secuencias = request.POST.getlist('op_secuencia[]')
        op_nombres = request.POST.getlist('op_nombre[]')
        op_descripciones = request.POST.getlist('op_descripcion[]')
        op_lineas = request.POST.getlist('op_linea[]')
        op_tiempos = request.POST.getlist('op_tiempo[]')
        op_unidades = request.POST.getlist('op_unidad_tiempo[]')
        op_maquinas = request.POST.getlist('op_maquina[]')
        op_operadores = request.POST.getlist('op_operadores[]')

        form_data.update({
            'codigo': codigo, 'producto': producto, 'version': version,
            'descripcion': descripcion, 'cantidad_base': cantidad_base_texto,
            'unidad_producto': unidad_producto, 'activo': activo,
            'componentes': [], 'operaciones': [],
        })

        try:
            cantidad_base = Decimal(cantidad_base_texto.replace(',', '.'))
        except InvalidOperation:
            cantidad_base = None

        error = None
        if not codigo:
            error = 'El código BOM es obligatorio.'
        elif not producto:
            error = 'El nombre del producto es obligatorio.'
        elif not version:
            error = 'La versión es obligatoria.'
        elif cantidad_base is None or cantidad_base <= 0:
            error = 'La cantidad base debe ser un número mayor a cero.'
        elif BOM.objects.filter(codigo__iexact=codigo, version__iexact=version).exists():
            error = 'Ya existe un BOM con ese código y versión.'

        if error:
            messages.error(request, error)
            return render(request, 'produccion/bom_mfg.html', {
                'materiales_catalogo': materiales_catalogo, 'boms_mfg': boms_mfg,
                'bom_kpis': bom_kpis, 'lineas': LINEAS, 'unidades_tiempo': UNIDADES_TIEMPO,
                'form_data': form_data,
            })

        materiales_validos = {str(m.id): m for m in materiales_catalogo}
        componentes = []
        materiales_usados = set()
        total_rows = max(len(materiales_ids), len(cantidades), len(observaciones_list))

        for idx in range(total_rows):
            material_id = (materiales_ids[idx] if idx < len(materiales_ids) else '').strip()
            cantidad_texto = (cantidades[idx] if idx < len(cantidades) else '').strip()
            obs = (observaciones_list[idx] if idx < len(observaciones_list) else '').strip()

            row_has_data = any([material_id, cantidad_texto, obs])
            if not row_has_data:
                continue

            form_data['componentes'].append({'material_id': material_id, 'cantidad': cantidad_texto, 'observaciones': obs})

            material_obj = materiales_validos.get(material_id)
            if not material_obj:
                messages.error(request, f'Fila {idx + 1}: selecciona un material válido.')
                componentes = []
                break
            if material_id in materiales_usados:
                messages.error(request, f'Fila {idx + 1}: material {material_obj.sku} duplicado.')
                componentes = []
                break
            try:
                cantidad = Decimal(cantidad_texto.replace(',', '.'))
            except InvalidOperation:
                messages.error(request, f'Fila {idx + 1}: la cantidad debe ser numérica.')
                componentes = []
                break
            if cantidad <= 0:
                messages.error(request, f'Fila {idx + 1}: la cantidad debe ser mayor a cero.')
                componentes = []
                break
            materiales_usados.add(material_id)
            componentes.append({'material': material_obj, 'cantidad': cantidad, 'observaciones': obs})

        operaciones = []
        if not messages.get_messages(request) or componentes is not None:
            total_ops = max(len(op_nombres), len(op_secuencias))
            for idx in range(total_ops):
                nombre_op = (op_nombres[idx] if idx < len(op_nombres) else '').strip()
                secuencia_texto = (op_secuencias[idx] if idx < len(op_secuencias) else '').strip()
                desc_op = (op_descripciones[idx] if idx < len(op_descripciones) else '').strip()
                linea_op = (op_lineas[idx] if idx < len(op_lineas) else '').strip()
                tiempo_texto = (op_tiempos[idx] if idx < len(op_tiempos) else '').strip()
                unidad_op = (op_unidades[idx] if idx < len(op_unidades) else 'min').strip()
                maquina_op = (op_maquinas[idx] if idx < len(op_maquinas) else '').strip()
                operadores_texto = (op_operadores[idx] if idx < len(op_operadores) else '1').strip()

                row_has_data = any([nombre_op, secuencia_texto, tiempo_texto, maquina_op])
                if not row_has_data:
                    continue

                form_data['operaciones'].append({
                    'secuencia': secuencia_texto, 'nombre': nombre_op, 'descripcion': desc_op,
                    'linea_produccion': linea_op, 'tiempo_estimado': tiempo_texto,
                    'unidad_tiempo': unidad_op, 'recurso_maquina': maquina_op,
                    'operadores_requeridos': operadores_texto,
                })

                if not nombre_op or not secuencia_texto:
                    messages.error(request, f'Operación {idx + 1}: nombre y secuencia son obligatorios.')
                    operaciones = []
                    break
                try:
                    secuencia = int(secuencia_texto)
                    operadores = max(1, int(operadores_texto or 1))
                except (ValueError, InvalidOperation):
                    messages.error(request, f'Operación {idx + 1}: secuencia y operadores deben ser numéricos.')
                    operaciones = []
                    break

                tiempo = None
                if tiempo_texto:
                    tiempo_normalizado = tiempo_texto.replace(',', '.').strip()
                    if not re.fullmatch(r'\d+', tiempo_normalizado):
                        messages.error(request, f'Operación {idx + 1}: el tiempo debe ser un número entero (sin decimales).')
                        operaciones = []
                        break
                    tiempo = Decimal(int(tiempo_normalizado))

                operaciones.append({
                    'secuencia': secuencia, 'nombre': nombre_op, 'descripcion': desc_op,
                    'linea_produccion': linea_op, 'tiempo_estimado': tiempo,
                    'unidad_tiempo': unidad_op, 'recurso_maquina': maquina_op,
                    'operadores_requeridos': operadores,
                })

        if not form_data['componentes']:
            form_data['componentes'] = [{'material_id': '', 'cantidad': '', 'observaciones': ''}]
        if not form_data['operaciones']:
            form_data['operaciones'] = [{'secuencia': '1', 'nombre': '', 'descripcion': '', 'linea_produccion': '',
                                          'tiempo_estimado': '', 'unidad_tiempo': 'min', 'recurso_maquina': '', 'operadores_requeridos': '1'}]

        if not list(messages.get_messages(request)) and componentes:
            with transaction.atomic():
                bom = BOM.objects.create(
                    codigo=codigo, tipo=BOM.TipoBOM.MFG, producto=producto,
                    version=version, descripcion=descripcion, cantidad_base=cantidad_base,
                    unidad_producto=unidad_producto, activo=activo, creado_por=request.user,
                )
                for comp in componentes:
                    BOMDetalle.objects.create(
                        bom=bom, material=comp['material'],
                        cantidad=comp['cantidad'], observaciones=comp['observaciones'],
                    )
                for op in operaciones:
                    BOMOperacion.objects.create(bom=bom, **op)

            messages.success(request, f'BOM MFG {bom.codigo} v{bom.version} creado correctamente.')
            return redirect('bom_mfg')

    return render(request, 'produccion/bom_mfg.html', {
        'materiales_catalogo': materiales_catalogo,
        'boms_mfg': boms_mfg,
        'bom_kpis': bom_kpis,
        'lineas': LINEAS,
        'unidades_tiempo': UNIDADES_TIEMPO,
        'form_data': form_data,
    })


def _next_plan_produccion_folio():
    current_year = date.today().year
    prefix = f"PP-{current_year}-"
    last_folio = (
        PlanProduccion.objects
        .filter(folio__startswith=prefix)
        .order_by('-id')
        .values_list('folio', flat=True)
        .first()
    )
    seq = 1
    if last_folio:
        match = re.match(rf"^{re.escape(prefix)}(\d+)$", last_folio)
        if match:
            seq = int(match.group(1)) + 1
    return f"{prefix}{seq:04d}"


def _next_requerimiento_material_folio():
    current_year = date.today().year
    prefix = f"RMP-{current_year}-"
    last_folio = (
        RequerimientoMaterialProduccion.objects
        .filter(folio__startswith=prefix)
        .order_by('-id')
        .values_list('folio', flat=True)
        .first()
    )
    seq = 1
    if last_folio:
        match = re.match(rf"^{re.escape(prefix)}(\d+)$", last_folio)
        if match:
            seq = int(match.group(1)) + 1
    return f"{prefix}{seq:04d}"


def _parse_decimal_text(value, default=Decimal('0')):
    try:
        return Decimal(str(value).strip().replace(',', '.'))
    except (InvalidOperation, AttributeError):
        return default


def _build_material_requirements(bom_obj, cantidad_planificada):
    if not bom_obj or bom_obj.cantidad_base <= 0:
        return []

    factor = cantidad_planificada / bom_obj.cantidad_base
    detalles = []

    for componente in bom_obj.componentes.select_related('material').all():
        material = componente.material
        cantidad_requerida = (componente.cantidad * factor).quantize(Decimal('0.001'))
        stock_disponible = (material.stock_actual or Decimal('0')).quantize(Decimal('0.001'))

        if cantidad_requerida > 0:
            ratio_suministro = stock_disponible / cantidad_requerida
            scrap_factor = Decimal('0.10') if ratio_suministro < Decimal('0.20') else Decimal('0.05')
        else:
            scrap_factor = Decimal('0.05')

        cantidad_con_scrap = (cantidad_requerida * (Decimal('1') + scrap_factor)).quantize(Decimal('0.001'))
        sugerida_compra = max(cantidad_con_scrap - stock_disponible, Decimal('0')).quantize(Decimal('0.001'))
        faltante_base = max(cantidad_requerida - stock_disponible, Decimal('0')).quantize(Decimal('0.001'))

        if stock_disponible >= cantidad_requerida:
            estado = PlanProduccionDetalle.EstadoMaterial.DISPONIBLE
            disponible = cantidad_requerida
            faltante = Decimal('0')
        elif stock_disponible > 0:
            estado = PlanProduccionDetalle.EstadoMaterial.PARCIAL
            disponible = stock_disponible
            faltante = faltante_base
        else:
            estado = PlanProduccionDetalle.EstadoMaterial.REQUIERE_COMPRA
            disponible = Decimal('0')
            faltante = cantidad_requerida

        detalles.append({
            'material': material,
            'cantidad_requerida': cantidad_requerida,
            'cantidad_disponible': disponible,
            'cantidad_faltante': faltante,
            'estado_material': estado,
            'stock_actual': stock_disponible,
            'scrap_factor': scrap_factor,
            'cantidad_con_scrap': cantidad_con_scrap,
            'cantidad_sugerida_compra': sugerida_compra,
        })

    return detalles


@login_required(login_url='login')
@never_cache
def planificacion_produccion(request):
    boms_activos = BOM.objects.filter(activo=True, tipo=BOM.TipoBOM.MFG).prefetch_related('componentes__material', 'operaciones').order_by('producto')

    def _build_planes_recientes():
        planes = (
            PlanProduccion.objects
            .select_related('bom', 'creado_por')
            .prefetch_related('detalles__material')
            .order_by('-fecha_creacion')[:10]
        )
        data = []
        for plan in planes:
            detalles = list(plan.detalles.all())
            total_requeridos = len(detalles)
            requieren_compra = sum(1 for d in detalles if d.estado_material == PlanProduccionDetalle.EstadoMaterial.REQUIERE_COMPRA)
            parciales = sum(1 for d in detalles if d.estado_material == PlanProduccionDetalle.EstadoMaterial.PARCIAL)
            data.append({
                'id': plan.id,
                'folio': plan.folio,
                'producto': plan.bom.producto,
                'bom_codigo': plan.bom.codigo,
                'cantidad_planificada': plan.cantidad_planificada,
                'fecha_inicio': plan.fecha_inicio,
                'fecha_fin': plan.fecha_fin,
                'linea_produccion': plan.linea_produccion,
                'turno': plan.turno,
                'estado': plan.estado,
                'estado_label': plan.get_estado_display(),
                'total_materiales': total_requeridos,
                'requieren_compra': requieren_compra,
                'parciales': parciales,
                'detalles': [
                    {
                        'sku': d.material.sku,
                        'nombre': d.material.nombre,
                        'um': d.material.um,
                        'cantidad_requerida': d.cantidad_requerida,
                        'cantidad_disponible': d.cantidad_disponible,
                        'cantidad_faltante': d.cantidad_faltante,
                        'estado_material': d.estado_material,
                        'estado_material_label': d.get_estado_material_display(),
                    }
                    for d in detalles
                ],
            })
        return data

    LINEAS_PRODUCCION = ['Línea SMT-01', 'Línea SMT-02', 'Línea SMT-03']
    TURNOS = ['Turno 1 (6:00 - 14:00)', 'Turno 2 (14:00 - 22:00)', 'Turno 3 (22:00 - 6:00)']

    if request.method == 'POST':
        bom_id = (request.POST.get('bom') or '').strip()
        cantidad_texto = (request.POST.get('cantidad_planificada') or '').strip()
        fecha_inicio_raw = (request.POST.get('fecha_inicio') or '').strip()
        fecha_fin_raw = (request.POST.get('fecha_fin') or '').strip()
        linea_produccion = (request.POST.get('linea_produccion') or '').strip()
        turno = (request.POST.get('turno') or '').strip()
        observaciones = (request.POST.get('observaciones') or '').strip()
        accion = (request.POST.get('accion') or 'borrador').strip().lower()

        bom_obj = BOM.objects.filter(id=bom_id, activo=True).prefetch_related('componentes__material').first()
        fecha_inicio = _parse_iso_date(fecha_inicio_raw)
        fecha_fin = _parse_iso_date(fecha_fin_raw)

        if not bom_obj:
            messages.error(request, 'Selecciona un BOM/producto válido del catálogo.')
        elif not cantidad_texto:
            messages.error(request, 'La cantidad a producir es obligatoria.')
        elif not fecha_inicio:
            messages.error(request, 'La fecha de inicio es obligatoria.')
        elif not fecha_fin:
            messages.error(request, 'La fecha de fin estimada es obligatoria.')
        elif fecha_fin < fecha_inicio:
            messages.error(request, 'La fecha fin no puede ser menor a la fecha de inicio.')
        else:
            try:
                cantidad_planificada = Decimal(cantidad_texto.replace(',', '.'))
            except InvalidOperation:
                messages.error(request, 'La cantidad debe ser un número válido.')
                return render(request, 'produccion/planificacion_produccion.html', {
                    'boms_activos': boms_activos,
                    'planes_recientes': _build_planes_recientes(),
                    'lineas_produccion': LINEAS_PRODUCCION,
                    'turnos': TURNOS,
                })

            if cantidad_planificada <= 0:
                messages.error(request, 'La cantidad a producir debe ser mayor a cero.')
            else:
                detalles_plan = _build_material_requirements(bom_obj, cantidad_planificada)

                faltantes = [d for d in detalles_plan if d['cantidad_faltante'] > 0]
                if faltantes:
                    nombres_faltantes = ', '.join(d['material'].sku for d in faltantes[:5])
                    if len(faltantes) > 5:
                        nombres_faltantes += f' y {len(faltantes) - 5} más'
                    messages.error(
                        request,
                        f'No se puede crear el plan: inventario insuficiente para {len(faltantes)} material(es) '
                        f'({nombres_faltantes}). Genera un Requerimiento de Materiales y envíalo a finanzas primero.'
                    )
                    return render(request, 'produccion/planificacion_produccion.html', {
                        'boms_activos': boms_activos,
                        'planes_recientes': _build_planes_recientes(),
                        'lineas_produccion': LINEAS_PRODUCCION,
                        'turnos': TURNOS,
                        'bloqueo_bom_id': bom_id,
                        'bloqueo_cantidad': cantidad_planificada,
                    })

                estado_destino = (
                    PlanProduccion.EstadoPlan.APROBADO
                    if accion == 'aprobar'
                    else PlanProduccion.EstadoPlan.BORRADOR
                )

                with transaction.atomic():
                    folio = _next_plan_produccion_folio()
                    while PlanProduccion.objects.filter(folio=folio).exists():
                        folio = _next_plan_produccion_folio()

                    plan = PlanProduccion.objects.create(
                        folio=folio,
                        bom=bom_obj,
                        cantidad_planificada=cantidad_planificada,
                        fecha_inicio=fecha_inicio,
                        fecha_fin=fecha_fin,
                        linea_produccion=linea_produccion,
                        turno=turno,
                        observaciones=observaciones,
                        estado=estado_destino,
                        creado_por=request.user,
                    )

                    for detalle in detalles_plan:
                        PlanProduccionDetalle.objects.create(
                            plan=plan,
                            material=detalle['material'],
                            cantidad_requerida=detalle['cantidad_requerida'],
                            cantidad_disponible=detalle['cantidad_disponible'],
                            cantidad_faltante=detalle['cantidad_faltante'],
                            estado_material=detalle['estado_material'],
                        )

                messages.success(request, f'Plan {plan.folio} creado en estado {plan.get_estado_display()}.')
                return redirect('planificacion_produccion')

    return render(request, 'produccion/planificacion_produccion.html', {
        'boms_activos': boms_activos,
        'planes_recientes': _build_planes_recientes(),
        'lineas_produccion': LINEAS_PRODUCCION,
        'turnos': TURNOS,
    })


@login_required(login_url='login')
@never_cache
def requerimiento_materiales_produccion(request):
    bom_id = (request.GET.get('bom') or '').strip()
    cantidad_texto = (request.GET.get('cantidad') or '').strip()

    bom_obj = (
        BOM.objects
        .filter(id=bom_id, activo=True, tipo=BOM.TipoBOM.MFG)
        .prefetch_related('componentes__material')
        .first()
    ) if bom_id else None

    if cantidad_texto:
        cantidad_planificada = _parse_decimal_text(cantidad_texto, Decimal('-1'))
    else:
        cantidad_planificada = Decimal('-1')

    if not bom_obj or cantidad_planificada <= 0:
        messages.error(request, 'Debes seleccionar un BOM y cantidad válida para generar requerimiento de materiales.')
        return redirect('planificacion_produccion')

    detalles = _build_material_requirements(bom_obj, cantidad_planificada)
    hay_faltantes = any(d['cantidad_faltante'] > 0 for d in detalles)

    if request.method == 'POST':
        accion = (request.POST.get('accion') or 'guardar').strip().lower()
        notas = (request.POST.get('notas') or '').strip()
        cantidades_solicitadas = request.POST.getlist('cantidad_solicitada[]')
        observaciones_items = request.POST.getlist('observacion[]')

        estado_req = (
            RequerimientoMaterialProduccion.EstadoRequerimiento.ENVIADO_FINANZAS
            if accion == 'enviar_finanzas'
            else RequerimientoMaterialProduccion.EstadoRequerimiento.BORRADOR
        )

        with transaction.atomic():
            folio = _next_requerimiento_material_folio()
            while RequerimientoMaterialProduccion.objects.filter(folio=folio).exists():
                folio = _next_requerimiento_material_folio()

            requerimiento = RequerimientoMaterialProduccion.objects.create(
                folio=folio,
                bom=bom_obj,
                cantidad_planificada=cantidad_planificada,
                notas=notas,
                estado=estado_req,
                creado_por=request.user,
                fecha_envio_finanzas=timezone.now() if estado_req == RequerimientoMaterialProduccion.EstadoRequerimiento.ENVIADO_FINANZAS else None,
            )

            for idx, detalle in enumerate(detalles):
                sugerida = detalle['cantidad_sugerida_compra']
                solicitada_input = cantidades_solicitadas[idx] if idx < len(cantidades_solicitadas) else ''
                solicitada = _parse_decimal_text(solicitada_input, sugerida)
                if solicitada < 0:
                    solicitada = Decimal('0')
                solicitada = solicitada.quantize(Decimal('0.001'))
                observacion = (observaciones_items[idx] if idx < len(observaciones_items) else '').strip()

                RequerimientoMaterialProduccionDetalle.objects.create(
                    requerimiento=requerimiento,
                    material=detalle['material'],
                    cantidad_base_requerida=detalle['cantidad_requerida'],
                    cantidad_con_scrap=detalle['cantidad_con_scrap'],
                    stock_actual=detalle['stock_actual'],
                    cantidad_sugerida_compra=sugerida,
                    cantidad_solicitada=solicitada,
                    observaciones=observacion,
                )

        if estado_req == RequerimientoMaterialProduccion.EstadoRequerimiento.ENVIADO_FINANZAS:
            messages.success(request, f'Requerimiento {requerimiento.folio} enviado a finanzas correctamente.')
        else:
            messages.success(request, f'Requerimiento {requerimiento.folio} guardado en borrador.')

        from django.urls import reverse
        return redirect(reverse('requerimiento_materiales_produccion') + f'?bom={bom_obj.id}&cantidad={cantidad_planificada}')

    requerimientos_recientes = (
        RequerimientoMaterialProduccion.objects
        .select_related('bom', 'creado_por')
        .prefetch_related('detalles__material')
        .order_by('-fecha_creacion')[:8]
    )

    return render(request, 'produccion/requerimiento_materiales_produccion.html', {
        'bom_obj': bom_obj,
        'cantidad_planificada': cantidad_planificada,
        'detalles': detalles,
        'hay_faltantes': hay_faltantes,
        'requerimientos_recientes': requerimientos_recientes,
    })


# ─── ÓRDENES DE FABRICACIÓN ───────────────────────────────────────────────────

def _next_of_folio():
    current_year = date.today().year
    prefix = f"OF-{current_year}-"
    last_folio = (
        OrdenFabricacion.objects
        .filter(folio__startswith=prefix)
        .order_by('-id')
        .values_list('folio', flat=True)
        .first()
    )
    seq = 1
    if last_folio:
        match = re.match(rf"^{re.escape(prefix)}(\d+)$", last_folio)
        if match:
            seq = int(match.group(1)) + 1
    return f"{prefix}{seq:04d}"


def _of_transiciones_permitidas(estado_actual):
    """Devuelve los estados destino válidos desde el estado actual."""
    transiciones = {
        OrdenFabricacion.EstadoOF.BORRADOR: [
            OrdenFabricacion.EstadoOF.EN_PROCESO,
            OrdenFabricacion.EstadoOF.CANCELADA,
        ],
        OrdenFabricacion.EstadoOF.EN_PROCESO: [
            OrdenFabricacion.EstadoOF.PAUSADA,
            OrdenFabricacion.EstadoOF.COMPLETADA,
            OrdenFabricacion.EstadoOF.CANCELADA,
        ],
        OrdenFabricacion.EstadoOF.PAUSADA: [
            OrdenFabricacion.EstadoOF.EN_PROCESO,
            OrdenFabricacion.EstadoOF.CANCELADA,
        ],
        OrdenFabricacion.EstadoOF.COMPLETADA: [],
        OrdenFabricacion.EstadoOF.CANCELADA: [],
    }
    return transiciones.get(estado_actual, [])


@login_required(login_url='login')
@never_cache
def ordenes_fabricacion(request):
    LINEAS_PRODUCCION = ['Línea SMT-01', 'Línea SMT-02', 'Línea SMT-03']
    TURNOS = ['Turno 1 (6:00 - 14:00)', 'Turno 2 (14:00 - 22:00)', 'Turno 3 (22:00 - 6:00)']

    boms_activos = (
        BOM.objects
        .filter(activo=True, tipo=BOM.TipoBOM.MFG)
        .prefetch_related('componentes__material')
        .order_by('producto')
    )
    planes_aprobados = (
        PlanProduccion.objects
        .filter(estado__in=[PlanProduccion.EstadoPlan.APROBADO, PlanProduccion.EstadoPlan.EN_PROCESO])
        .select_related('bom')
        .order_by('-fecha_creacion')
    )

    def _build_ofs_recientes():
        ofs = (
            OrdenFabricacion.objects
            .select_related('bom', 'plan', 'creado_por')
            .prefetch_related('detalles__material')
            .order_by('-fecha_creacion')[:15]
        )
        data = []
        for of in ofs:
            detalles = list(of.detalles.all())
            avance = 0
            if of.cantidad_planificada > 0:
                avance = min(int(round(float(of.cantidad_producida / of.cantidad_planificada) * 100)), 100)
            data.append({
                'id': of.id,
                'folio': of.folio,
                'producto': of.bom.producto,
                'bom_codigo': of.bom.codigo,
                'plan_folio': of.plan.folio if of.plan else None,
                'cantidad_planificada': of.cantidad_planificada,
                'cantidad_producida': of.cantidad_producida,
                'avance': avance,
                'linea_produccion': of.linea_produccion,
                'turno': of.turno,
                'estado': of.estado,
                'estado_label': of.get_estado_display(),
                'fecha_inicio_programada': of.fecha_inicio_programada,
                'fecha_fin_programada': of.fecha_fin_programada,
                'fecha_inicio_real': of.fecha_inicio_real,
                'fecha_fin_real': of.fecha_fin_real,
                'transiciones': [t for t in _of_transiciones_permitidas(of.estado)],
                'detalles': [
                    {
                        'sku': d.material.sku,
                        'nombre': d.material.nombre,
                        'um': d.material.um,
                        'cantidad_requerida': d.cantidad_requerida,
                        'cantidad_consumida': d.cantidad_consumida,
                    }
                    for d in detalles
                ],
            })
        return data

    # ── POST: cambio de estado ─────────────────────────────────────────────────
    if request.method == 'POST' and (request.POST.get('accion_estado') or '').strip():
        of_id = (request.POST.get('of_id') or '').strip()
        accion_estado = (request.POST.get('accion_estado') or '').strip()
        of_obj = OrdenFabricacion.objects.filter(id=of_id).first()

        if not of_obj:
            messages.error(request, 'La orden de fabricación no existe.')
            return redirect('ordenes_fabricacion')

        transiciones = _of_transiciones_permitidas(of_obj.estado)
        if accion_estado not in transiciones:
            messages.error(
                request,
                f'No se puede cambiar de {of_obj.get_estado_display()} a {accion_estado}.',
            )
            return redirect('ordenes_fabricacion')

        update_fields = ['estado', 'fecha_actualizacion']

        if accion_estado == OrdenFabricacion.EstadoOF.EN_PROCESO and not of_obj.fecha_inicio_real:
            of_obj.fecha_inicio_real = timezone.now()
            update_fields.append('fecha_inicio_real')

        if accion_estado == OrdenFabricacion.EstadoOF.COMPLETADA:
            cantidad_producida_raw = (request.POST.get('cantidad_producida') or '').strip()
            cantidad_producida = _parse_decimal_text(cantidad_producida_raw, of_obj.cantidad_planificada)
            if cantidad_producida < 0:
                cantidad_producida = Decimal('0')
            of_obj.cantidad_producida = cantidad_producida.quantize(Decimal('0.01'))
            of_obj.fecha_fin_real = timezone.now()
            update_fields += ['cantidad_producida', 'fecha_fin_real']

            # Actualizar consumos registrados por el usuario
            material_ids = request.POST.getlist('consumo_material_id[]')
            cantidades_consumidas = request.POST.getlist('consumo_cantidad[]')
            for idx, mat_id in enumerate(material_ids):
                cant_texto = cantidades_consumidas[idx] if idx < len(cantidades_consumidas) else ''
                cant = _parse_decimal_text(cant_texto, Decimal('0'))
                if cant < 0:
                    cant = Decimal('0')
                OrdenFabricacionDetalle.objects.filter(
                    orden=of_obj, material_id=mat_id
                ).update(cantidad_consumida=cant.quantize(Decimal('0.001')))

            # Actualizar estado del plan asociado a EN_PROCESO si aún está en APROBADO
            if of_obj.plan and of_obj.plan.estado == PlanProduccion.EstadoPlan.APROBADO:
                PlanProduccion.objects.filter(id=of_obj.plan_id).update(
                    estado=PlanProduccion.EstadoPlan.EN_PROCESO
                )

        of_obj.estado = accion_estado
        of_obj.save(update_fields=update_fields)

        # Si se completó la OF, actualizar plan a COMPLETADO si todas sus OFs están completadas
        if accion_estado == OrdenFabricacion.EstadoOF.COMPLETADA and of_obj.plan:
            plan = of_obj.plan
            todas_completadas = not plan.ordenes_fabricacion.exclude(
                estado__in=[
                    OrdenFabricacion.EstadoOF.COMPLETADA,
                    OrdenFabricacion.EstadoOF.CANCELADA,
                ]
            ).exists()
            if todas_completadas:
                PlanProduccion.objects.filter(id=plan.id).update(
                    estado=PlanProduccion.EstadoPlan.COMPLETADO
                )

        messages.success(
            request,
            f'Orden {of_obj.folio} actualizada a estado {of_obj.get_estado_display()}.',
        )
        return redirect('ordenes_fabricacion')

    # ── POST: crear nueva OF ───────────────────────────────────────────────────
    if request.method == 'POST':
        plan_id = (request.POST.get('plan_id') or '').strip()
        bom_id = (request.POST.get('bom_id') or '').strip()
        cantidad_texto = (request.POST.get('cantidad_planificada') or '').strip()
        linea_produccion = (request.POST.get('linea_produccion') or '').strip()
        turno = (request.POST.get('turno') or '').strip()
        fecha_inicio_raw = (request.POST.get('fecha_inicio_programada') or '').strip()
        fecha_fin_raw = (request.POST.get('fecha_fin_programada') or '').strip()
        observaciones = (request.POST.get('observaciones') or '').strip()

        plan_obj = PlanProduccion.objects.filter(
            id=plan_id,
            estado__in=[PlanProduccion.EstadoPlan.APROBADO, PlanProduccion.EstadoPlan.EN_PROCESO],
        ).select_related('bom').prefetch_related('bom__componentes__material').first() if plan_id else None

        bom_obj = None
        if plan_obj:
            bom_obj = plan_obj.bom
            if not cantidad_texto:
                cantidad_texto = str(plan_obj.cantidad_planificada)
            if not fecha_inicio_raw:
                fecha_inicio_raw = plan_obj.fecha_inicio.isoformat()
            if not fecha_fin_raw:
                fecha_fin_raw = plan_obj.fecha_fin.isoformat()
            if not linea_produccion:
                linea_produccion = plan_obj.linea_produccion
            if not turno:
                turno = plan_obj.turno
        elif bom_id:
            bom_obj = BOM.objects.filter(
                id=bom_id, activo=True, tipo=BOM.TipoBOM.MFG
            ).prefetch_related('componentes__material').first()

        fecha_inicio = _parse_iso_date(fecha_inicio_raw)
        fecha_fin = _parse_iso_date(fecha_fin_raw)

        if not bom_obj:
            messages.error(request, 'Selecciona un Plan de Producción aprobado o un BOM MFG válido.')
        elif not cantidad_texto:
            messages.error(request, 'La cantidad a producir es obligatoria.')
        else:
            try:
                cantidad_planificada = Decimal(cantidad_texto.replace(',', '.'))
            except InvalidOperation:
                messages.error(request, 'La cantidad debe ser un número válido.')
                return render(request, 'produccion/ordenes_fabricacion.html', {
                    'boms_activos': boms_activos,
                    'planes_aprobados': planes_aprobados,
                    'ofs_recientes': _build_ofs_recientes(),
                    'lineas_produccion': LINEAS_PRODUCCION,
                    'turnos': TURNOS,
                })

            if cantidad_planificada <= 0:
                messages.error(request, 'La cantidad debe ser mayor a cero.')
            else:
                detalles_requeridos = _build_material_requirements(bom_obj, cantidad_planificada)

                with transaction.atomic():
                    folio = _next_of_folio()
                    while OrdenFabricacion.objects.filter(folio=folio).exists():
                        folio = _next_of_folio()

                    of_nuevo = OrdenFabricacion.objects.create(
                        folio=folio,
                        plan=plan_obj,
                        bom=bom_obj,
                        cantidad_planificada=cantidad_planificada,
                        linea_produccion=linea_produccion,
                        turno=turno,
                        fecha_inicio_programada=fecha_inicio,
                        fecha_fin_programada=fecha_fin,
                        observaciones=observaciones,
                        estado=OrdenFabricacion.EstadoOF.BORRADOR,
                        creado_por=request.user,
                    )

                    for detalle in detalles_requeridos:
                        OrdenFabricacionDetalle.objects.create(
                            orden=of_nuevo,
                            material=detalle['material'],
                            cantidad_requerida=detalle['cantidad_requerida'],
                        )

                    # Actualizar estado del plan a EN_PROCESO si está en APROBADO
                    if plan_obj and plan_obj.estado == PlanProduccion.EstadoPlan.APROBADO:
                        PlanProduccion.objects.filter(id=plan_obj.id).update(
                            estado=PlanProduccion.EstadoPlan.EN_PROCESO
                        )

                messages.success(request, f'Orden de fabricación {of_nuevo.folio} creada correctamente.')
                return redirect('ordenes_fabricacion')

    return render(request, 'produccion/ordenes_fabricacion.html', {
        'boms_activos': boms_activos,
        'planes_aprobados': planes_aprobados,
        'ofs_recientes': _build_ofs_recientes(),
        'lineas_produccion': LINEAS_PRODUCCION,
        'turnos': TURNOS,
        'estado_en_proceso': OrdenFabricacion.EstadoOF.EN_PROCESO,
        'estado_pausada': OrdenFabricacion.EstadoOF.PAUSADA,
        'estado_completada': OrdenFabricacion.EstadoOF.COMPLETADA,
        'estado_cancelada': OrdenFabricacion.EstadoOF.CANCELADA,
        'estado_borrador': OrdenFabricacion.EstadoOF.BORRADOR,
    })


# ─────────────────────────────────────────────────────────────
#  CAPTURA DE LOTES DE PRODUCCIÓN
# ─────────────────────────────────────────────────────────────
TURNOS_LOTE = ['Turno 1 Mañana', 'Turno 2 Tarde', 'Turno 3 Noche']
LINEAS_LOTE = [
    'Linea Metalmecanica 1', 'Linea Metalmecanica 2',
    'Linea Ensamble 1', 'Linea Ensamble 2',
    'Linea Acabados', 'Linea Empaque', 'Linea QA',
]


def _next_lote_folio():
    current_year = date.today().year
    prefix = f"LP-{current_year}-"
    last = (
        LoteProduccion.objects
        .filter(folio__startswith=prefix)
        .order_by('-id')
        .values_list('folio', flat=True)
        .first()
    )
    seq = 1
    if last:
        m = re.match(rf"^{re.escape(prefix)}(\d+)$", last)
        if m:
            seq = int(m.group(1)) + 1
    return f"{prefix}{seq:04d}"


@login_required
def captura_lotes(request):
    if request.method == 'POST':
        action = request.POST.get('action', '')

        # ── CREAR LOTE ──
        if action == 'crear':
            bom_id      = request.POST.get('bom_id', '').strip()
            of_id       = request.POST.get('of_id', '').strip()
            fecha_raw   = request.POST.get('fecha_captura', '').strip()
            hora_raw    = request.POST.get('hora_captura', '').strip()
            linea       = request.POST.get('linea_produccion', '').strip()
            turno       = request.POST.get('turno', '').strip()
            cantidad    = request.POST.get('cantidad_producida', '').strip()
            operador    = request.POST.get('operador', '').strip()
            obs         = request.POST.get('observaciones', '').strip()

            errors = []
            if not bom_id:
                errors.append('Selecciona un producto (BOM).')
            if not fecha_raw:
                errors.append('La fecha de captura es obligatoria.')
            if not hora_raw:
                errors.append('La hora de captura es obligatoria.')
            if not cantidad:
                errors.append('La cantidad producida es obligatoria.')
            else:
                try:
                    cantidad_dec = Decimal(cantidad.replace(',', '.'))
                    if cantidad_dec <= 0:
                        errors.append('La cantidad debe ser mayor a 0.')
                except Exception:
                    errors.append('La cantidad producida no es válida.')
                    cantidad_dec = None

            if not errors:
                try:
                    bom_obj = BOM.objects.get(id=bom_id, activo=True)
                except BOM.DoesNotExist:
                    errors.append('Producto (BOM) no encontrado.')
                    bom_obj = None

                of_obj = None
                if of_id:
                    try:
                        of_obj = OrdenFabricacion.objects.get(id=of_id)
                    except OrdenFabricacion.DoesNotExist:
                        pass

                fecha_cap = _parse_iso_date(fecha_raw)
                if not fecha_cap:
                    errors.append('Fecha de captura inválida.')

                try:
                    hora_cap = datetime.strptime(hora_raw, '%H:%M').time()
                except ValueError:
                    hora_cap = None
                    errors.append('Hora inválida (use HH:MM).')

            if not errors and bom_obj and fecha_cap and hora_cap:
                folio = _next_lote_folio()
                LoteProduccion.objects.create(
                    folio=folio,
                    bom=bom_obj,
                    orden_fabricacion=of_obj,
                    fecha_captura=fecha_cap,
                    hora_captura=hora_cap,
                    linea_produccion=linea,
                    turno=turno,
                    cantidad_producida=cantidad_dec,
                    operador=operador,
                    estado=LoteProduccion.EstadoLote.CAPTURADO,
                    observaciones=obs,
                    creado_por=request.user,
                )
                # Si hay OF vinculada, actualizar cantidad_producida de la OF
                if of_obj:
                    of_obj.cantidad_producida = (of_obj.cantidad_producida or Decimal('0')) + cantidad_dec
                    if of_obj.cantidad_producida >= of_obj.cantidad_planificada:
                        of_obj.estado = OrdenFabricacion.EstadoOF.COMPLETADA
                        if not of_obj.fecha_fin_real:
                            of_obj.fecha_fin_real = timezone.now()
                    of_obj.save()
                return JsonResponse({'ok': True, 'folio': folio})
            return JsonResponse({'ok': False, 'errors': errors})

        # ── CAMBIAR ESTADO DE LOTE ──
        if action == 'cambiar_estado':
            lote_id  = request.POST.get('lote_id', '').strip()
            nuevo_estado = request.POST.get('estado', '').strip()
            estados_validos = {e.value for e in LoteProduccion.EstadoLote}
            if nuevo_estado not in estados_validos:
                return JsonResponse({'ok': False, 'error': 'Estado inválido.'})
            try:
                lote = LoteProduccion.objects.get(id=lote_id)
                lote.estado = nuevo_estado
                lote.save()
                return JsonResponse({'ok': True})
            except LoteProduccion.DoesNotExist:
                return JsonResponse({'ok': False, 'error': 'Lote no encontrado.'})

        return JsonResponse({'ok': False, 'error': 'Acción no reconocida.'})

    # ── GET ──
    lotes = (
        LoteProduccion.objects
        .select_related('bom', 'orden_fabricacion', 'creado_por')
        .order_by('-fecha_captura', '-hora_captura')[:200]
    )
    boms_mfg = BOM.objects.filter(activo=True, tipo=BOM.TipoBOM.MFG).order_by('producto')
    ofs_activas = (
        OrdenFabricacion.objects
        .filter(estado__in=[OrdenFabricacion.EstadoOF.EN_PROCESO, OrdenFabricacion.EstadoOF.BORRADOR])
        .select_related('bom')
        .order_by('folio')
    )

    total_lotes  = LoteProduccion.objects.count()
    total_uds    = LoteProduccion.objects.aggregate(t=Sum('cantidad_producida'))['t'] or 0
    lotes_hoy    = LoteProduccion.objects.filter(fecha_captura=date.today()).count()
    lotes_validados = LoteProduccion.objects.filter(estado=LoteProduccion.EstadoLote.VALIDADO).count()

    return render(request, 'produccion/captura_lotes.html', {
        'lotes': lotes,
        'boms_mfg': boms_mfg,
        'ofs_activas': ofs_activas,
        'turnos': TURNOS_LOTE,
        'lineas': LINEAS_LOTE,
        'total_lotes': total_lotes,
        'total_uds': float(total_uds),
        'lotes_hoy': lotes_hoy,
        'lotes_validados': lotes_validados,
        'estado_capturado': LoteProduccion.EstadoLote.CAPTURADO,
        'estado_validado': LoteProduccion.EstadoLote.VALIDADO,
        'estado_rechazado': LoteProduccion.EstadoLote.RECHAZADO,
    })


# ─────────────────────────────────────────────────────────────
#  ESCANEO DE PRODUCCIÓN REALIZADA
# ─────────────────────────────────────────────────────────────
@login_required
def escaneo_produccion(request):
    if request.method == 'POST':
        action = request.POST.get('action', '')

        # ── BUSCAR OF POR FOLIO ──
        if action == 'buscar_of':
            folio = request.POST.get('folio', '').strip().upper()
            try:
                of = OrdenFabricacion.objects.select_related('bom', 'plan').get(folio=folio)
                avance_pct = 0
                if of.cantidad_planificada and of.cantidad_planificada > 0:
                    avance_pct = min(100, int(round(float(of.cantidad_producida / of.cantidad_planificada * 100))))
                lotes_of = list(
                    LoteProduccion.objects.filter(orden_fabricacion=of)
                    .order_by('-fecha_captura', '-hora_captura')
                    .values('folio', 'fecha_captura', 'hora_captura', 'turno', 'cantidad_producida', 'estado', 'operador')
                )
                for lt in lotes_of:
                    lt['fecha_captura'] = lt['fecha_captura'].isoformat()
                    lt['hora_captura'] = lt['hora_captura'].strftime('%H:%M')
                return JsonResponse({
                    'ok': True,
                    'of': {
                        'id': of.id,
                        'folio': of.folio,
                        'producto': of.bom.producto,
                        'bom_codigo': of.bom.codigo,
                        'linea': of.linea_produccion,
                        'turno': of.turno,
                        'estado': of.get_estado_display(),
                        'estado_raw': of.estado,
                        'cantidad_planificada': float(of.cantidad_planificada),
                        'cantidad_producida': float(of.cantidad_producida),
                        'avance_pct': avance_pct,
                        'fecha_inicio': of.fecha_inicio_programada.isoformat() if of.fecha_inicio_programada else '',
                        'fecha_fin': of.fecha_fin_programada.isoformat() if of.fecha_fin_programada else '',
                    },
                    'lotes': lotes_of,
                })
            except OrdenFabricacion.DoesNotExist:
                return JsonResponse({'ok': False, 'error': f'No se encontró la OF "{folio}".'})

        # ── REGISTRAR PAQUETE DE PRODUCCIÓN ──
        if action == 'registrar':
            of_id      = request.POST.get('of_id', '').strip()
            cantidad   = request.POST.get('cantidad', '').strip()
            turno      = request.POST.get('turno', '').strip()
            operador   = request.POST.get('operador', '').strip()
            obs        = request.POST.get('observaciones', '').strip()
            fecha_raw  = request.POST.get('fecha', '').strip()
            hora_raw   = request.POST.get('hora', '').strip()

            errors = []
            try:
                of = OrdenFabricacion.objects.select_related('bom').get(id=of_id)
            except OrdenFabricacion.DoesNotExist:
                return JsonResponse({'ok': False, 'error': 'OF no encontrada.'})

            try:
                cantidad_dec = Decimal(cantidad.replace(',', '.'))
                if cantidad_dec <= 0:
                    errors.append('La cantidad debe ser mayor a 0.')
            except Exception:
                errors.append('Cantidad inválida.')
                cantidad_dec = None

            fecha_cap = _parse_iso_date(fecha_raw) or date.today()
            try:
                hora_cap = datetime.strptime(hora_raw, '%H:%M').time()
            except ValueError:
                hora_cap = datetime.now().time().replace(second=0, microsecond=0)

            if errors:
                return JsonResponse({'ok': False, 'errors': errors})

            folio_lote = _next_lote_folio()
            LoteProduccion.objects.create(
                folio=folio_lote,
                bom=of.bom,
                orden_fabricacion=of,
                fecha_captura=fecha_cap,
                hora_captura=hora_cap,
                linea_produccion=of.linea_produccion,
                turno=turno or of.turno,
                cantidad_producida=cantidad_dec,
                operador=operador,
                estado=LoteProduccion.EstadoLote.CAPTURADO,
                observaciones=obs,
                creado_por=request.user,
            )

            # Actualizar OF
            of.cantidad_producida = (of.cantidad_producida or Decimal('0')) + cantidad_dec
            if of.cantidad_producida >= of.cantidad_planificada:
                of.estado = OrdenFabricacion.EstadoOF.COMPLETADA
                if not of.fecha_fin_real:
                    of.fecha_fin_real = timezone.now()
            elif of.estado == OrdenFabricacion.EstadoOF.BORRADOR:
                of.estado = OrdenFabricacion.EstadoOF.EN_PROCESO
                if not of.fecha_inicio_real:
                    of.fecha_inicio_real = timezone.now()
            of.save()

            avance_pct = min(100, int(round(float(of.cantidad_producida / of.cantidad_planificada * 100))))
            return JsonResponse({
                'ok': True,
                'folio_lote': folio_lote,
                'cantidad_producida': float(of.cantidad_producida),
                'avance_pct': avance_pct,
                'estado_raw': of.estado,
                'estado': of.get_estado_display(),
            })

        return JsonResponse({'ok': False, 'error': 'Acción no reconocida.'})

    # ── GET ──
    ofs_activas = (
        OrdenFabricacion.objects
        .filter(estado__in=[
            OrdenFabricacion.EstadoOF.BORRADOR,
            OrdenFabricacion.EstadoOF.EN_PROCESO,
            OrdenFabricacion.EstadoOF.PAUSADA,
        ])
        .select_related('bom')
        .order_by('folio')
    )
    ultimos_registros = (
        LoteProduccion.objects
        .filter(orden_fabricacion__isnull=False)
        .select_related('bom', 'orden_fabricacion')
        .order_by('-fecha_captura', '-hora_captura')[:30]
    )
    return render(request, 'produccion/escaneo_produccion.html', {
        'ofs_activas': ofs_activas,
        'ultimos_registros': ultimos_registros,
        'turnos': TURNOS_LOTE,
    })

