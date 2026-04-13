from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout, get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from datetime import date, timedelta
from django.db import IntegrityError
from django.db import transaction
from django.db.models import Case, Count, DecimalField, Q, Sum, Value, When
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from collections import defaultdict
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from .models import (
    Almacen,
    InventarioAlmacen,
    Material,
    Proveedor,
    RecepcionMaterial,
    RecepcionMaterialDetalle,
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
def proveedores_alta(request):
    materiales_catalogo = Material.objects.filter(activo=True).order_by('sku')
    proveedores_recientes = Proveedor.objects.prefetch_related('materiales').order_by('-fecha_creacion')[:8]

    form_data = {
        'nombre': '',
        'descripcion': '',
        'telefono': '',
        'email': '',
        'activo': True,
        'materiales_ids': [],
    }

    if request.method == 'POST':
        nombre = (request.POST.get('nombre') or '').strip()
        descripcion = (request.POST.get('descripcion') or '').strip()
        telefono = (request.POST.get('telefono') or '').strip()
        email = (request.POST.get('email') or '').strip()
        activo = (request.POST.get('activo') or '1').strip() == '1'
        materiales_ids = [item for item in request.POST.getlist('materiales[]') if item]

        form_data.update({
            'nombre': nombre,
            'descripcion': descripcion,
            'telefono': telefono,
            'email': email,
            'activo': activo,
            'materiales_ids': materiales_ids,
        })

        if not nombre:
            messages.error(request, 'El nombre del proveedor es obligatorio.')
        elif Proveedor.objects.filter(nombre__iexact=nombre).exists():
            messages.error(request, 'Ya existe un proveedor con ese nombre.')
        else:
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
    
    # Obtener los materiales del proveedor
    materiales = proveedor.materiales.filter(activo=True).values('sku', 'nombre', 'descripcion', 'um')
    
    # Construir el JSON en el formato esperado por el frontend
    materiales_dict = {
        m['sku']: {
            'sku': m['sku'],
            'nombre': m['nombre'],
            'descripcion': m['descripcion'],
            'um': m['um'],
        }
        for m in materiales
    }
    
    return JsonResponse(materiales_dict)
