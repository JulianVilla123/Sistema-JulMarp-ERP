from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout, get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from datetime import date, datetime, timedelta
from django.db import IntegrityError
from django.db import transaction
from django.db.models import Case, Count, DecimalField, Q, Sum, Value, When
from django.db.models.deletion import ProtectedError
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from collections import defaultdict
from urllib.parse import urlencode
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from .models import (
    Almacen,
    BitacoraAcceso,
    BOM,
    BOMDetalle,
    BOMOperacion,
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
    InventarioAlmacen,
    InformeValidacionDefectoQA,
    LoteProduccion,
    MovimientoContable,
    Material,
    OrdenCompra,
    OrdenCompraDetalle,
    PolizaContable,
    PresupuestoFinanciero,
    Proveedor,
    ProveedorMaterialPrecio,
    ReclamoCliente,
    RecepcionMaterial,
    RecepcionMaterialDetalle,
    RegistroScrapDefecto,
    RegistroUsoRecursoProduccion,
    ReporteFinanciero,
    OrdenFabricacion,
    OrdenFabricacionDetalle,
    PlanProduccion,
    PlanProduccionDetalle,
    RequerimientoMaterialProduccion,
    RequerimientoMaterialProduccionDetalle,
    ReporteKPIProduccion,
    SalidaLinea,
    SalidaLineaDetalle,
    TransferenciaAlmacen,
    TransferenciaAlmacenDetalle,
    TicketSoporte,
    UsuarioERP,
)
from .kpi_produccion import (
    calcular_kpis_produccion,
    estado_kpi,
    exportar_reporte_excel,
    exportar_reporte_pdf,
    generar_reporte_kpis_produccion,
)
from .finanzas import (
    calcular_dashboard_finanzas,
    consolidar_costeos_produccion,
    exportar_reporte_financiero_excel,
    exportar_reporte_financiero_pdf,
    generar_estado_financiero,
    generar_reporte_financiero,
)

# Create your views here.

User = get_user_model()


def _mark_dashboard_sync(request, scope='global'):
    request.session['dashboard_sync_token'] = timezone.now().isoformat()
    request.session['dashboard_sync_scope'] = scope
    request.session.modified = True


def _usuario_puede_administrar_clientes(user):
    if not user.is_authenticated:
        return False

    departamento = user.departamento.nombre if user.departamento else ''
    return departamento in {'IT', 'Admin'}


def _usuario_puede_crear_usuarios(user):
    if not user.is_authenticated:
        return False

    departamento = user.departamento.nombre if user.departamento else ''
    return departamento in {'IT', 'Admin'}


def _client_ip(request):
    forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded_for:
        return forwarded_for.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def _registrar_acceso(request, username, exitoso, usuario=None, accion='login'):
    BitacoraAcceso.objects.create(
        usuario=usuario,
        usuario_ingresado=(username or '').strip(),
        exitoso=exitoso,
        accion=accion,
        ip=_client_ip(request),
        user_agent=request.META.get('HTTP_USER_AGENT', '')[:1000],
    )


def _registrar_cambio_usuario(usuario_afectado, realizado_por, accion, detalle=''):
    HistorialCambioUsuario.objects.create(
        usuario_afectado=usuario_afectado,
        realizado_por=realizado_por,
        accion=accion,
        detalle=detalle,
    )


def _next_ticket_folio():
    current_year = date.today().year
    prefix = f"SOP-{current_year}-"
    last_folio = (
        TicketSoporte.objects
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


def _usuario_puede_ver_kpis_mfg(user):
    if not user.is_authenticated:
        return False

    departamento = user.departamento.nombre if user.departamento else ''
    return departamento in {'Producción', 'Finanzas', 'Admin', 'IT'}


def _usuario_puede_control_recursos_mfg(user):
    if not user.is_authenticated:
        return False

    departamento = user.departamento.nombre if user.departamento else ''
    return departamento in {'Producción', 'RRHH', 'Admin', 'IT'}


def _usuario_puede_validar_defectos(user):
    if not user.is_authenticated:
        return False

    departamento = user.departamento.nombre if user.departamento else ''
    return departamento in {'QA', 'Calidad', 'Admin', 'IT'}


def _usuario_puede_ver_finanzas(user):
    if not user.is_authenticated:
        return False

    departamento = user.departamento.nombre if user.departamento else ''
    return departamento in {'Finanzas', 'Admin', 'IT'}


def _finance_date_range(request, default_days=29):
    fecha_fin = timezone.localdate()
    fecha_inicio = fecha_fin - timedelta(days=default_days)
    fecha_inicio_raw = (request.GET.get('fecha_inicio') or request.POST.get('fecha_inicio') or '').strip()
    fecha_fin_raw = (request.GET.get('fecha_fin') or request.POST.get('fecha_fin') or '').strip()

    parsed_inicio = _parse_iso_date(fecha_inicio_raw) if fecha_inicio_raw else fecha_inicio
    parsed_fin = _parse_iso_date(fecha_fin_raw) if fecha_fin_raw else fecha_fin

    if parsed_inicio and parsed_fin and parsed_inicio > parsed_fin:
        parsed_inicio, parsed_fin = parsed_fin, parsed_inicio

    return parsed_inicio or fecha_inicio, parsed_fin or fecha_fin


def _next_generic_folio(model_class, prefix):
    current_year = date.today().year
    full_prefix = f"{prefix}-{current_year}-"
    last_folio = (
        model_class.objects
        .filter(folio__startswith=full_prefix)
        .order_by('-id')
        .values_list('folio', flat=True)
        .first()
    )
    seq = 1
    if last_folio:
        match = re.match(rf"^{re.escape(full_prefix)}(\d+)$", last_folio)
        if match:
            seq = int(match.group(1)) + 1
    return f"{full_prefix}{seq:04d}"


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
            _registrar_acceso(request, username_input, True, usuario=user)
            login(request, user)
            return redirect('home')  # Redirigir a home después del login
        else:
            _registrar_acceso(request, username_input, False)
            messages.error(request, 'Usuario o contraseña incorrectos.')
    return render(request, 'authentication/login.html')


@login_required(login_url='login')
@never_cache
def register_usuario(request):
    if not _usuario_puede_crear_usuarios(request.user):
        messages.error(request, 'No tienes permisos para crear usuarios.')
        return redirect('home')

    departamentos = Departamento.objects.filter(activo=True).order_by('nombre')

    if request.method == 'POST':
        username_input = (request.POST.get('username') or '').strip()
        first_name = (request.POST.get('first_name') or '').strip()
        last_name = (request.POST.get('last_name') or '').strip()
        email = (request.POST.get('email') or '').strip()
        telefono = (request.POST.get('telefono') or '').strip()
        numero_empleado = (request.POST.get('numero_empleado') or '').strip()
        departamento_id = (request.POST.get('departamento') or '').strip()
        password = request.POST.get('password')
        confirm_password = request.POST.get('confirm_password')
        departamento_obj = Departamento.objects.filter(id=departamento_id, activo=True).first() if departamento_id else None

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
                user.departamento = departamento_obj
                user.save()
                _registrar_cambio_usuario(
                    user,
                    request.user,
                    'Creación de usuario',
                    f'Usuario creado desde IT. Departamento: {departamento_obj.nombre if departamento_obj else "Sin departamento"}.',
                )

                if username_input != username:
                    messages.success(request, f'Usuario asignado automáticamente: {username}')

                messages.success(request, 'Usuario creado exitosamente.')
                return redirect('register')
            except IntegrityError:
                messages.error(request, 'El número de empleado ya está registrado.')

        form_data = {
            'username': username,
            'first_name': first_name,
            'last_name': last_name,
            'email': email,
            'telefono': telefono,
            'numero_empleado': numero_empleado,
            'departamento': departamento_id,
        }
        return render(request, 'authentication/register.html', {'form_data': form_data, 'departamentos': departamentos})

    return render(request, 'authentication/register.html', {'departamentos': departamentos})


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

    if departamento in {'QA', 'Calidad'}:
        ahora = timezone.now()
        fecha_inicio_semana = ahora - timedelta(days=7)

        qa_sqa_pendientes = RecepcionMaterial.objects.filter(
            estado=RecepcionMaterial.EstadoRecepcion.ENVIADA,
            chk_calidad=False,
        ).count()
        qa_sqa_revisadas_semana = RecepcionMaterial.objects.filter(
            chk_calidad=True,
            fecha_creacion__gte=fecha_inicio_semana,
        ).count()
        qa_sqa_cuarentena = RecepcionMaterial.objects.filter(
            chk_calidad=True,
            accion_recomendada=RecepcionMaterial.AccionRecomendada.CUARENTENA,
        ).count()
        qa_sqa_liberadas = RecepcionMaterial.objects.filter(
            chk_calidad=True,
            accion_recomendada=RecepcionMaterial.AccionRecomendada.ACEPTAR_TODO,
        ).count()

        qa_oqa_pendientes = LoteProduccion.objects.filter(
            estado=LoteProduccion.EstadoLote.CAPTURADO,
        ).count()
        qa_oqa_liberados_semana = LoteProduccion.objects.filter(
            estado=LoteProduccion.EstadoLote.VALIDADO,
            fecha_actualizacion__gte=fecha_inicio_semana,
        ).count()
        qa_oqa_retenidos = LoteProduccion.objects.filter(
            estado=LoteProduccion.EstadoLote.RECHAZADO,
        ).count()

        qa_reclamos_abiertos = ReclamoCliente.objects.exclude(
            estado_reclamo=ReclamoCliente.EstadoReclamo.CERRADO,
        ).count()
        qa_reclamos_criticos = ReclamoCliente.objects.filter(
            prioridad=ReclamoCliente.PrioridadReclamo.ALTA,
        ).exclude(
            estado_reclamo=ReclamoCliente.EstadoReclamo.CERRADO,
        ).count()
        qa_reclamos_cerrados_semana = ReclamoCliente.objects.filter(
            estado_reclamo=ReclamoCliente.EstadoReclamo.CERRADO,
            fecha_actualizacion__gte=fecha_inicio_semana,
        ).count()

        qa_sqa_recientes = (
            RecepcionMaterial.objects
            .filter(chk_calidad=True)
            .order_by('-fecha_creacion')[:6]
        )
        qa_oqa_recientes = (
            LoteProduccion.objects
            .filter(estado__in=[
                LoteProduccion.EstadoLote.VALIDADO,
                LoteProduccion.EstadoLote.RECHAZADO,
            ])
            .select_related('bom', 'orden_fabricacion')
            .order_by('-fecha_actualizacion')[:6]
        )
        qa_reclamos_recientes = (
            ReclamoCliente.objects
            .order_by('-fecha_actualizacion')[:6]
        )

        context.update({
            'qa_sqa_pendientes': qa_sqa_pendientes,
            'qa_sqa_revisadas_semana': qa_sqa_revisadas_semana,
            'qa_sqa_cuarentena': qa_sqa_cuarentena,
            'qa_oqa_pendientes': qa_oqa_pendientes,
            'qa_oqa_liberados_semana': qa_oqa_liberados_semana,
            'qa_reclamos_abiertos': qa_reclamos_abiertos,
            'qa_reclamos_criticos': qa_reclamos_criticos,
            'qa_sqa_recientes': qa_sqa_recientes,
            'qa_oqa_recientes': qa_oqa_recientes,
            'qa_reclamos_recientes': qa_reclamos_recientes,
            'qa_chart_data': {
                'pipeline_labels': ['SQA pendientes', 'OQA pendientes', 'Reclamos abiertos'],
                'pipeline_values': [qa_sqa_pendientes, qa_oqa_pendientes, qa_reclamos_abiertos],
                'status_labels': ['SQA liberadas', 'SQA cuarentena', 'OQA liberados', 'OQA retenidos', 'Reclamos cerrados'],
                'status_values': [
                    qa_sqa_liberadas,
                    qa_sqa_cuarentena,
                    qa_oqa_liberados_semana,
                    qa_oqa_retenidos,
                    qa_reclamos_cerrados_semana,
                ],
            },
        })

    if departamento == 'IT':
        clientes_activos = ClienteCompra.objects.filter(activo=True).count()
        clientes_inactivos = ClienteCompra.objects.filter(activo=False).count()
        clientes_recientes = ClienteCompra.objects.order_by('-fecha_actualizacion')[:6]
        usuarios_activos = User.objects.filter(is_active=True, activo=True).count()
        usuarios_bloqueados = User.objects.filter(Q(is_active=False) | Q(activo=False)).count()
        usuarios_sin_departamento = User.objects.filter(departamento__isnull=True).count()
        usuarios_staff = User.objects.filter(is_staff=True).count()
        usuarios_recientes = (
            User.objects
            .select_related('departamento')
            .order_by('-fecha_creacion')[:6]
        )
        usuarios_por_departamento = (
            Departamento.objects
            .filter(activo=True)
            .annotate(total_usuarios=Count('usuarioerp'))
            .order_by('nombre')
        )
        tickets_abiertos = TicketSoporte.objects.filter(
            estado__in=[TicketSoporte.Estado.NUEVO, TicketSoporte.Estado.EN_PROCESO]
        ).count()
        tickets_criticos = TicketSoporte.objects.filter(
            prioridad=TicketSoporte.Prioridad.CRITICA
        ).exclude(estado__in=[TicketSoporte.Estado.RESUELTO, TicketSoporte.Estado.CANCELADO]).count()
        accesos_fallidos = BitacoraAcceso.objects.filter(exitoso=False).count()
        accesos_recientes = BitacoraAcceso.objects.select_related('usuario').order_by('-fecha')[:6]
        tickets_recientes = TicketSoporte.objects.select_related('solicitado_por').order_by('-fecha_actualizacion')[:6]

        context.update({
            'it_clientes_activos': clientes_activos,
            'it_clientes_inactivos': clientes_inactivos,
            'it_clientes_recientes': clientes_recientes,
            'it_usuarios_activos': usuarios_activos,
            'it_usuarios_bloqueados': usuarios_bloqueados,
            'it_usuarios_sin_departamento': usuarios_sin_departamento,
            'it_usuarios_staff': usuarios_staff,
            'it_usuarios_recientes': usuarios_recientes,
            'it_usuarios_por_departamento': usuarios_por_departamento,
            'it_tickets_abiertos': tickets_abiertos,
            'it_tickets_criticos': tickets_criticos,
            'it_accesos_fallidos': accesos_fallidos,
            'it_accesos_recientes': accesos_recientes,
            'it_tickets_recientes': tickets_recientes,
        })

    if departamento == 'Finanzas':
        finance_data = calcular_dashboard_finanzas()
        context.update({
            'fin_kpis': finance_data['kpis'],
            'fin_alertas': finance_data['alertas'],
            'fin_ordenes_compra_recientes': finance_data['ordenes_compra_recientes'],
            'fin_costeos_recientes': finance_data['costeos_recientes'],
            'fin_cuentas_recientes': finance_data['cuentas_recientes'],
            'fin_presupuestos': finance_data['presupuestos'],
            'fin_chart_data': finance_data['charts'],
            'fin_produccion': finance_data['produccion'],
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


@login_required(login_url='login')
@never_cache
def indicadores_kpis_mfg(request):
    if not _usuario_puede_ver_kpis_mfg(request.user):
        messages.error(request, 'No tienes permisos para consultar los KPIs de MFG.')
        return redirect('home')

    fecha_fin = timezone.localdate()
    fecha_inicio = fecha_fin - timedelta(days=29)

    fecha_inicio_raw = (request.GET.get('fecha_inicio') or '').strip()
    fecha_fin_raw = (request.GET.get('fecha_fin') or '').strip()
    export = (request.GET.get('export') or '').strip().lower()
    recalcular = request.GET.get('recalcular') == '1'

    try:
        if fecha_inicio_raw:
            fecha_inicio = date.fromisoformat(fecha_inicio_raw)
        if fecha_fin_raw:
            fecha_fin = date.fromisoformat(fecha_fin_raw)
    except ValueError:
        messages.error(request, 'El rango de fechas no es válido. Se usó el período por defecto de 30 días.')
        fecha_fin = timezone.localdate()
        fecha_inicio = fecha_fin - timedelta(days=29)

    if fecha_inicio > fecha_fin:
        fecha_inicio, fecha_fin = fecha_fin, fecha_inicio

    reporte = (
        ReporteKPIProduccion.objects
        .filter(fecha_inicio=fecha_inicio, fecha_fin=fecha_fin)
        .order_by('-fecha_generacion')
        .first()
    )

    if recalcular or export or reporte is None:
        reporte = generar_reporte_kpis_produccion(
            usuario=request.user,
            fecha_inicio=fecha_inicio,
            fecha_fin=fecha_fin,
        )

    if export == 'excel':
        payload = exportar_reporte_excel(reporte)
        response = HttpResponse(
            payload,
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        response['Content-Disposition'] = f'attachment; filename="kpis-mfg-{reporte.fecha_inicio}-{reporte.fecha_fin}.xlsx"'
        return response

    if export == 'pdf':
        payload = exportar_reporte_pdf(reporte)
        response = HttpResponse(payload, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="kpis-mfg-{reporte.fecha_inicio}-{reporte.fecha_fin}.pdf"'
        return response

    detail = reporte.detalle or {}
    cycle_ideal = Decimal(str(detail.get('tiempo_ciclo_ideal', 0)))
    variacion_pct = Decimal(str(detail.get('costo_variacion_pct', 0)))

    metric_cards = [
        {
            'code': 'oee',
            'label': 'OEE',
            'value': reporte.oee,
            'unit': '%',
            'status': estado_kpi('oee', reporte.oee),
            'help_text': f"Disp. {reporte.disponibilidad}% | Rend. {reporte.rendimiento}% | Calidad {reporte.calidad}%",
        },
        {
            'code': 'tiempo_ciclo',
            'label': 'Tiempo de ciclo',
            'value': reporte.tiempo_ciclo_promedio,
            'unit': 'min/ud',
            'status': estado_kpi('tiempo_ciclo', reporte.tiempo_ciclo_promedio, cycle_ideal),
            'help_text': f"Ideal BOM: {cycle_ideal.quantize(Decimal('0.01'))} min/ud" if cycle_ideal > 0 else 'Sin tiempo ideal configurado en BOM.',
        },
        {
            'code': 'tasa_rechazo',
            'label': 'Tasa de rechazo',
            'value': reporte.tasa_rechazo,
            'unit': '%',
            'status': estado_kpi('tasa_rechazo', reporte.tasa_rechazo),
            'help_text': f"Unidades rechazadas: {detail.get('unidades_rechazadas', 0)}",
        },
        {
            'code': 'cumplimiento_ordenes',
            'label': 'Cumplimiento de órdenes',
            'value': reporte.cumplimiento_ordenes,
            'unit': '%',
            'status': estado_kpi('cumplimiento_ordenes', reporte.cumplimiento_ordenes),
            'help_text': f"{detail.get('ordenes_a_tiempo', 0)} / {detail.get('ordenes_completadas', 0)} OFs a tiempo",
        },
        {
            'code': 'variacion_costos_pct',
            'label': 'Costos vs plan',
            'value': variacion_pct.quantize(Decimal('0.01')),
            'unit': '%',
            'status': estado_kpi('variacion_costos_pct', variacion_pct),
            'help_text': f"Plan ${reporte.costo_planificado} | Real ${reporte.costo_real}",
        },
        {
            'code': 'utilizacion_recursos',
            'label': 'Utilización recursos',
            'value': reporte.utilizacion_recursos,
            'unit': '%',
            'status': estado_kpi('utilizacion_recursos', reporte.utilizacion_recursos),
            'help_text': f"Máquinas {reporte.utilizacion_maquinas}% | Personal {reporte.utilizacion_personal}%",
        },
    ]

    chart_data = {
        'metric_labels': [card['label'] for card in metric_cards],
        'metric_values': [float(card['value']) for card in metric_cards],
        'metric_status': [card['status'] for card in metric_cards],
        'resource_labels': ['Máquinas', 'Personal'],
        'resource_values': [float(reporte.utilizacion_maquinas), float(reporte.utilizacion_personal)],
        'cost_labels': ['Planificado', 'Real', 'Variación'],
        'cost_values': [float(reporte.costo_planificado), float(reporte.costo_real), float(reporte.variacion_costos)],
    }

    kpi_context = calcular_kpis_produccion(fecha_inicio=fecha_inicio, fecha_fin=fecha_fin)

    return render(
        request,
        'produccion/indicadores_kpis.html',
        {
            'reporte': reporte,
            'metric_cards': metric_cards,
            'chart_data': chart_data,
            'alertas': reporte.alertas or [],
            'detalle': detail,
            'ordenes_recientes': detail.get('ordenes_recientes', []),
            'fecha_inicio': fecha_inicio,
            'fecha_fin': fecha_fin,
            'kpi_snapshot': kpi_context,
        },
    )


@login_required(login_url='login')
@never_cache
def finanzas_dashboard(request):
    if not _usuario_puede_ver_finanzas(request.user):
        messages.error(request, 'No tienes permisos para acceder al dashboard de Finanzas.')
        return redirect('home')

    fecha_inicio, fecha_fin = _finance_date_range(request)
    export = (request.GET.get('export') or '').strip().lower()
    recalcular = (request.GET.get('recalcular') or '').strip() == '1'

    if recalcular:
        consolidar_costeos_produccion(usuario=request.user, fecha_inicio=fecha_inicio, fecha_fin=fecha_fin)

    reporte = (
        ReporteFinanciero.objects
        .filter(fecha_inicio=fecha_inicio, fecha_fin=fecha_fin, tipo=ReporteFinanciero.TipoReporte.KPI)
        .order_by('-fecha_creacion')
        .first()
    )
    if recalcular or reporte is None:
        reporte = generar_reporte_financiero(
            usuario=request.user,
            tipo=ReporteFinanciero.TipoReporte.KPI,
            fecha_inicio=fecha_inicio,
            fecha_fin=fecha_fin,
        )

    if export == 'excel':
        payload = exportar_reporte_financiero_excel(reporte)
        response = HttpResponse(payload, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = f'attachment; filename="finanzas-{reporte.fecha_inicio}-{reporte.fecha_fin}.xlsx"'
        return response

    if export == 'pdf':
        payload = exportar_reporte_financiero_pdf(reporte)
        response = HttpResponse(payload, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="finanzas-{reporte.fecha_inicio}-{reporte.fecha_fin}.pdf"'
        return response

    dashboard = calcular_dashboard_finanzas(fecha_inicio=fecha_inicio, fecha_fin=fecha_fin)
    return render(
        request,
        'finanzas/dashboard.html',
        {
            'reporte': reporte,
            'dashboard': dashboard,
            'chart_data': dashboard['charts'],
            'fecha_inicio': fecha_inicio,
            'fecha_fin': fecha_fin,
        },
    )


@login_required(login_url='login')
@never_cache
def finanzas_contabilidad(request):
    if not _usuario_puede_ver_finanzas(request.user):
        messages.error(request, 'No tienes permisos para acceder a Contabilidad.')
        return redirect('home')

    if request.method == 'POST':
        accion = (request.POST.get('accion') or '').strip()

        if accion == 'crear_cuenta':
            codigo = (request.POST.get('codigo') or '').strip().upper()
            nombre = (request.POST.get('nombre') or '').strip()
            tipo = (request.POST.get('tipo') or '').strip()
            descripcion = (request.POST.get('descripcion') or '').strip()
            activa = request.POST.get('activa') == 'on'

            if not codigo or not nombre or not tipo:
                messages.error(request, 'Código, nombre y tipo de cuenta son obligatorios.')
                return redirect('finanzas_contabilidad')

            cuenta, created = CuentaContable.objects.get_or_create(
                codigo=codigo,
                defaults={
                    'nombre': nombre,
                    'tipo': tipo,
                    'descripcion': descripcion,
                    'activa': activa,
                    'creado_por': request.user,
                    'actualizado_por': request.user,
                },
            )
            if not created:
                cuenta.nombre = nombre
                cuenta.tipo = tipo
                cuenta.descripcion = descripcion
                cuenta.activa = activa
                cuenta.actualizado_por = request.user
                cuenta.save()

            messages.success(request, f'Cuenta contable {cuenta.codigo} guardada correctamente.')
            return redirect('finanzas_contabilidad')

        if accion == 'crear_poliza':
            fecha_poliza = _parse_iso_date((request.POST.get('fecha_poliza') or '').strip())
            tipo = (request.POST.get('tipo_poliza') or '').strip() or PolizaContable.TipoPoliza.DIARIO
            concepto = (request.POST.get('concepto') or '').strip()
            referencia = (request.POST.get('referencia') or '').strip()
            cuentas_ids = request.POST.getlist('cuenta_id[]')
            descripciones = request.POST.getlist('mov_descripcion[]')
            debes = request.POST.getlist('debe[]')
            haberes = request.POST.getlist('haber[]')

            if not fecha_poliza or not concepto:
                messages.error(request, 'Fecha y concepto de la póliza son obligatorios.')
                return redirect('finanzas_contabilidad')

            movimientos = []
            total_debe = Decimal('0')
            total_haber = Decimal('0')
            for idx, cuenta_id in enumerate(cuentas_ids):
                cuenta_id = (cuenta_id or '').strip()
                debe = _parse_decimal_text(debes[idx] if idx < len(debes) else '0', Decimal('0'))
                haber = _parse_decimal_text(haberes[idx] if idx < len(haberes) else '0', Decimal('0'))
                descripcion = (descripciones[idx] if idx < len(descripciones) else '').strip()
                if not cuenta_id and debe <= 0 and haber <= 0:
                    continue
                cuenta = CuentaContable.objects.filter(id=cuenta_id, activa=True).first()
                if not cuenta:
                    messages.error(request, f'Línea {idx + 1}: selecciona una cuenta contable válida.')
                    return redirect('finanzas_contabilidad')
                if debe <= 0 and haber <= 0:
                    messages.error(request, f'Línea {idx + 1}: debe capturar debe u haber.')
                    return redirect('finanzas_contabilidad')
                movimientos.append({'cuenta': cuenta, 'descripcion': descripcion, 'debe': debe, 'haber': haber})
                total_debe += debe
                total_haber += haber

            if not movimientos or total_debe != total_haber:
                messages.error(request, 'La póliza debe contener movimientos balanceados; el debe debe ser igual al haber.')
                return redirect('finanzas_contabilidad')

            with transaction.atomic():
                folio = _next_generic_folio(PolizaContable, 'POL')
                while PolizaContable.objects.filter(folio=folio).exists():
                    folio = _next_generic_folio(PolizaContable, 'POL')
                poliza = PolizaContable.objects.create(
                    folio=folio,
                    fecha_poliza=fecha_poliza,
                    tipo=tipo,
                    concepto=concepto,
                    referencia=referencia,
                    estado=PolizaContable.EstadoPoliza.CONTABILIZADA,
                    creado_por=request.user,
                    actualizado_por=request.user,
                )
                for movimiento in movimientos:
                    MovimientoContable.objects.create(poliza=poliza, **movimiento)

            messages.success(request, f'Póliza {poliza.folio} contabilizada correctamente.')
            return redirect('finanzas_contabilidad')

        if accion == 'generar_estado':
            tipo_estado = (request.POST.get('tipo_estado') or '').strip() or EstadoFinanciero.TipoEstado.RESULTADOS
            fecha_inicio, fecha_fin = _finance_date_range(request)
            estado = generar_estado_financiero(request.user, fecha_inicio, fecha_fin, tipo_estado)
            messages.success(request, f'Se generó el estado financiero {estado.nombre}.')
            return redirect('finanzas_contabilidad')

    balances = list(
        CuentaContable.objects
        .filter(activa=True)
        .annotate(total_debe=Sum('movimientos__debe'), total_haber=Sum('movimientos__haber'))
        .order_by('codigo')
    )
    polizas = list(PolizaContable.objects.prefetch_related('movimientos__cuenta').order_by('-fecha_poliza', '-fecha_creacion')[:12])
    estados = list(EstadoFinanciero.objects.order_by('-fecha_fin', '-fecha_creacion')[:10])
    return render(request, 'finanzas/contabilidad.html', {
        'cuentas': CuentaContable.objects.order_by('codigo'),
        'balances': balances,
        'polizas': polizas,
        'estados_financieros': estados,
        'tipos_cuenta': CuentaContable.TipoCuenta.choices,
        'tipos_poliza': PolizaContable.TipoPoliza.choices,
        'tipos_estado': EstadoFinanciero.TipoEstado.choices,
    })


@login_required(login_url='login')
@never_cache
def finanzas_presupuestos(request):
    if not _usuario_puede_ver_finanzas(request.user):
        messages.error(request, 'No tienes permisos para acceder a Presupuestos.')
        return redirect('home')

    if request.method == 'POST':
        accion = (request.POST.get('accion') or '').strip()
        if accion == 'guardar_presupuesto':
            nombre = (request.POST.get('nombre') or '').strip()
            categoria = (request.POST.get('categoria') or '').strip()
            periodicidad = (request.POST.get('periodicidad') or '').strip() or PresupuestoFinanciero.Periodicidad.MENSUAL
            fecha_inicio = _parse_iso_date((request.POST.get('fecha_inicio') or '').strip())
            fecha_fin = _parse_iso_date((request.POST.get('fecha_fin') or '').strip())
            monto_presupuestado = _parse_decimal_text((request.POST.get('monto_presupuestado') or '').strip(), Decimal('0'))
            monto_real = _parse_decimal_text((request.POST.get('monto_real') or '').strip(), Decimal('0'))
            descripcion = (request.POST.get('descripcion') or '').strip()
            activo = request.POST.get('activo') == 'on'

            if not nombre or not fecha_inicio or not fecha_fin or monto_presupuestado <= 0:
                messages.error(request, 'Nombre, fechas y monto presupuestado son obligatorios.')
                return redirect('finanzas_presupuestos')

            PresupuestoFinanciero.objects.create(
                nombre=nombre,
                categoria=categoria,
                periodicidad=periodicidad,
                fecha_inicio=fecha_inicio,
                fecha_fin=fecha_fin,
                monto_presupuestado=monto_presupuestado,
                monto_real=monto_real,
                descripcion=descripcion,
                activo=activo,
                creado_por=request.user,
                actualizado_por=request.user,
            )
            messages.success(request, 'Presupuesto financiero registrado correctamente.')
            return redirect('finanzas_presupuestos')

        if accion == 'actualizar_real':
            presupuesto_id = (request.POST.get('presupuesto_id') or '').strip()
            monto_real = _parse_decimal_text((request.POST.get('monto_real_actualizado') or '').strip(), Decimal('0'))
            presupuesto = PresupuestoFinanciero.objects.filter(id=presupuesto_id).first()
            if not presupuesto:
                messages.error(request, 'El presupuesto seleccionado no existe.')
                return redirect('finanzas_presupuestos')
            presupuesto.monto_real = monto_real
            presupuesto.actualizado_por = request.user
            presupuesto.save()
            messages.success(request, f'Se actualizó el monto real del presupuesto {presupuesto.nombre}.')
            return redirect('finanzas_presupuestos')

    presupuestos = list(PresupuestoFinanciero.objects.order_by('-fecha_fin', 'nombre'))
    for presupuesto in presupuestos:
        presupuesto.desviacion = (presupuesto.monto_real or Decimal('0')) - (presupuesto.monto_presupuestado or Decimal('0'))

    return render(request, 'finanzas/presupuestos.html', {
        'presupuestos': presupuestos,
        'categorias': PresupuestoFinanciero.Categoria.choices,
        'periodicidades': PresupuestoFinanciero.Periodicidad.choices,
    })


@login_required(login_url='login')
@never_cache
def finanzas_pagos_cobros(request):
    if not _usuario_puede_ver_finanzas(request.user):
        messages.error(request, 'No tienes permisos para acceder a Pagos y Cobros.')
        return redirect('home')

    if request.method == 'POST':
        accion = (request.POST.get('accion') or '').strip()
        if accion == 'guardar_cuenta':
            tipo = (request.POST.get('tipo') or '').strip()
            folio = (request.POST.get('folio') or '').strip().upper() or _next_generic_folio(CuentaPorPagarCobrar, 'CXC')
            tercero_nombre = (request.POST.get('tercero_nombre') or '').strip()
            cliente_id = (request.POST.get('cliente_compra_id') or '').strip()
            proveedor_id = (request.POST.get('proveedor_id') or '').strip()
            orden_compra_id = (request.POST.get('orden_compra_id') or '').strip()
            monto_total = _parse_decimal_text((request.POST.get('monto_total') or '').strip(), Decimal('0'))
            fecha_emision = _parse_iso_date((request.POST.get('fecha_emision') or '').strip())
            fecha_vencimiento = _parse_iso_date((request.POST.get('fecha_vencimiento') or '').strip())
            observaciones = (request.POST.get('observaciones') or '').strip()

            if not tipo or not tercero_nombre or monto_total <= 0 or not fecha_emision or not fecha_vencimiento:
                messages.error(request, 'Tipo, tercero, fechas y monto total son obligatorios.')
                return redirect('finanzas_pagos_cobros')

            CuentaPorPagarCobrar.objects.create(
                tipo=tipo,
                folio=folio,
                tercero_nombre=tercero_nombre,
                cliente_compra=ClienteCompra.objects.filter(id=cliente_id).first() if cliente_id else None,
                proveedor=Proveedor.objects.filter(id=proveedor_id).first() if proveedor_id else None,
                orden_compra=OrdenCompra.objects.filter(id=orden_compra_id).first() if orden_compra_id else None,
                monto_total=monto_total,
                fecha_emision=fecha_emision,
                fecha_vencimiento=fecha_vencimiento,
                observaciones=observaciones,
                creado_por=request.user,
                actualizado_por=request.user,
            )
            messages.success(request, f'Cuenta financiera {folio} registrada correctamente.')
            return redirect('finanzas_pagos_cobros')

        if accion == 'registrar_movimiento':
            cuenta_id = (request.POST.get('cuenta_id') or '').strip()
            abono = _parse_decimal_text((request.POST.get('abono') or '').strip(), Decimal('0'))
            cuenta = CuentaPorPagarCobrar.objects.filter(id=cuenta_id).first()
            if not cuenta or abono <= 0:
                messages.error(request, 'Selecciona una cuenta válida y un abono mayor a cero.')
                return redirect('finanzas_pagos_cobros')
            cuenta.monto_pagado = min(cuenta.monto_pagado + abono, cuenta.monto_total)
            if cuenta.monto_pagado >= cuenta.monto_total:
                cuenta.estado = CuentaPorPagarCobrar.EstadoCuenta.PAGADA
            elif cuenta.fecha_vencimiento < timezone.localdate():
                cuenta.estado = CuentaPorPagarCobrar.EstadoCuenta.VENCIDA
            else:
                cuenta.estado = CuentaPorPagarCobrar.EstadoCuenta.PARCIAL
            cuenta.actualizado_por = request.user
            cuenta.save()
            messages.success(request, f'Se registró movimiento en la cuenta {cuenta.folio}.')
            return redirect('finanzas_pagos_cobros')

    cuentas = list(CuentaPorPagarCobrar.objects.select_related('cliente_compra', 'proveedor', 'orden_compra').order_by('fecha_vencimiento', '-fecha_creacion'))
    return render(request, 'finanzas/pagos_cobros.html', {
        'cuentas': cuentas,
        'tipos_cuenta': CuentaPorPagarCobrar.TipoCuenta.choices,
        'clientes_catalogo': ClienteCompra.objects.filter(activo=True).order_by('nombre'),
        'proveedores_catalogo': Proveedor.objects.filter(activo=True).order_by('nombre'),
        'ordenes_compra_catalogo': OrdenCompra.objects.order_by('-fecha_orden')[:30],
    })


@login_required(login_url='login')
@never_cache
def finanzas_costeo_produccion(request):
    if not _usuario_puede_ver_finanzas(request.user):
        messages.error(request, 'No tienes permisos para acceder a Costeo de Producción.')
        return redirect('home')

    fecha_inicio, fecha_fin = _finance_date_range(request)
    if request.method == 'POST' and (request.POST.get('accion') or '').strip() == 'recalcular_costeos':
        consolidar_costeos_produccion(request.user, fecha_inicio, fecha_fin)
        messages.success(request, 'Costeo de producción recalculado correctamente.')
        return redirect(f"{reverse('finanzas_costeo_produccion')}?fecha_inicio={fecha_inicio}&fecha_fin={fecha_fin}")

    costeos = list(
        CosteoProduccion.objects
        .select_related('orden_fabricacion', 'orden_fabricacion__bom', 'lote_produccion', 'lote_produccion__bom')
        .filter(fecha_actualizacion__date__range=(fecha_inicio, fecha_fin))
        .order_by('-fecha_actualizacion')[:40]
    )
    return render(request, 'finanzas/costeo_produccion.html', {
        'costeos': costeos,
        'fecha_inicio': fecha_inicio,
        'fecha_fin': fecha_fin,
    })


@login_required(login_url='login')
@never_cache
def finanzas_reportes(request):
    if not _usuario_puede_ver_finanzas(request.user):
        messages.error(request, 'No tienes permisos para acceder a Reportes Financieros.')
        return redirect('home')

    fecha_inicio, fecha_fin = _finance_date_range(request)
    export = (request.GET.get('export') or '').strip().lower()
    reporte_id = (request.GET.get('reporte_id') or '').strip()

    reporte = ReporteFinanciero.objects.filter(id=reporte_id).first() if reporte_id else None
    if request.method == 'POST' and (request.POST.get('accion') or '').strip() == 'generar_reporte':
        consolidar_costeos_produccion(request.user, fecha_inicio, fecha_fin)
        reporte = generar_reporte_financiero(request.user, ReporteFinanciero.TipoReporte.KPI, fecha_inicio, fecha_fin)
        messages.success(request, 'Reporte financiero generado correctamente.')
        return redirect(f"{reverse('finanzas_reportes')}?reporte_id={reporte.id}")

    if export and reporte:
        if export == 'excel':
            payload = exportar_reporte_financiero_excel(reporte)
            response = HttpResponse(payload, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            response['Content-Disposition'] = f'attachment; filename="reporte-financiero-{reporte.id}.xlsx"'
            return response
        if export == 'pdf':
            payload = exportar_reporte_financiero_pdf(reporte)
            response = HttpResponse(payload, content_type='application/pdf')
            response['Content-Disposition'] = f'attachment; filename="reporte-financiero-{reporte.id}.pdf"'
            return response

    reportes = list(ReporteFinanciero.objects.order_by('-fecha_fin', '-fecha_creacion')[:20])
    dashboard = calcular_dashboard_finanzas(fecha_inicio, fecha_fin)
    return render(request, 'finanzas/reportes_financieros.html', {
        'reportes': reportes,
        'reporte_actual': reporte,
        'dashboard': dashboard,
        'fecha_inicio': fecha_inicio,
        'fecha_fin': fecha_fin,
    })


@login_required(login_url='login')
@never_cache
def finanzas_impuestos(request):
    if not _usuario_puede_ver_finanzas(request.user):
        messages.error(request, 'No tienes permisos para acceder a Impuestos.')
        return redirect('home')

    if request.method == 'POST':
        accion = (request.POST.get('accion') or '').strip()
        if accion == 'generar_declaracion':
            folio = (request.POST.get('folio') or '').strip().upper() or _next_generic_folio(DeclaracionImpuesto, 'IMP')
            tipo_impuesto = (request.POST.get('tipo_impuesto') or '').strip()
            periodo_inicio = _parse_iso_date((request.POST.get('periodo_inicio') or '').strip())
            periodo_fin = _parse_iso_date((request.POST.get('periodo_fin') or '').strip())
            base_gravable = _parse_decimal_text((request.POST.get('base_gravable') or '').strip(), Decimal('0'))
            tasa = _parse_decimal_text((request.POST.get('tasa') or '').strip(), Decimal('0'))
            impuesto_calculado = (base_gravable * tasa / Decimal('100')).quantize(Decimal('0.01'))

            if not tipo_impuesto or not periodo_inicio or not periodo_fin:
                messages.error(request, 'Tipo de impuesto y periodo son obligatorios.')
                return redirect('finanzas_impuestos')

            DeclaracionImpuesto.objects.create(
                folio=folio,
                tipo_impuesto=tipo_impuesto,
                periodo_inicio=periodo_inicio,
                periodo_fin=periodo_fin,
                base_gravable=base_gravable,
                tasa=tasa,
                impuesto_calculado=impuesto_calculado,
                estado=DeclaracionImpuesto.EstadoDeclaracion.CALCULADA,
                detalle={'origen': 'manual'},
                creado_por=request.user,
                actualizado_por=request.user,
            )
            messages.success(request, f'Declaración {folio} calculada correctamente.')
            return redirect('finanzas_impuestos')

        if accion == 'presentar':
            declaracion_id = (request.POST.get('declaracion_id') or '').strip()
            acuse = (request.POST.get('acuse') or '').strip()
            declaracion = DeclaracionImpuesto.objects.filter(id=declaracion_id).first()
            if not declaracion:
                messages.error(request, 'La declaración seleccionada no existe.')
                return redirect('finanzas_impuestos')
            declaracion.estado = DeclaracionImpuesto.EstadoDeclaracion.PRESENTADA
            declaracion.acuse = acuse
            declaracion.actualizado_por = request.user
            declaracion.save()
            messages.success(request, f'Declaración {declaracion.folio} marcada como presentada.')
            return redirect('finanzas_impuestos')

    declaraciones = list(DeclaracionImpuesto.objects.order_by('-periodo_fin', '-fecha_creacion')[:20])
    return render(request, 'finanzas/impuestos.html', {
        'declaraciones': declaraciones,
        'tipos_impuesto': DeclaracionImpuesto.TipoImpuesto.choices,
    })


@login_required(login_url='login')
@never_cache
def finanzas_ordenes_compra(request):
    if not _usuario_puede_ver_finanzas(request.user):
        messages.error(request, 'No tienes permisos para acceder a Órdenes de Compra de Finanzas.')
        return redirect('home')

    requerimientos_pendientes_qs = (
        RequerimientoMaterialProduccion.objects
        .filter(estado=RequerimientoMaterialProduccion.EstadoRequerimiento.ENVIADO_FINANZAS)
        .exclude(ordenes_compra_generadas__creada_desde_mfg=True)
        .select_related('bom', 'creado_por')
        .prefetch_related('detalles__material')
        .order_by('-fecha_envio_finanzas', '-fecha_creacion')
        .distinct()
    )

    if request.method == 'POST' and (request.POST.get('accion_estado') or '').strip():
        orden_id = (request.POST.get('orden_id') or '').strip()
        accion_estado = (request.POST.get('accion_estado') or '').strip().upper()
        orden = OrdenCompra.objects.filter(id=orden_id).first()
        if not orden:
            messages.error(request, 'La orden seleccionada no existe.')
            return redirect('finanzas_ordenes_compra')
        transiciones = _ordenes_transiciones_permitidas(orden.estado)
        if accion_estado not in transiciones:
            messages.error(request, f'No es posible cambiar la orden a {accion_estado}.')
            return redirect('finanzas_ordenes_compra')
        orden.estado = accion_estado
        orden.save(update_fields=['estado'])
        messages.success(request, f'La orden {orden.folio} cambió a {orden.get_estado_display()}.')
        return redirect('finanzas_ordenes_compra')

    if request.method == 'POST':
        accion = (request.POST.get('accion') or '').strip()
        if accion == 'crear_oc_desde_mfg':
            requerimiento_id = (request.POST.get('requerimiento_id') or '').strip()
            proveedor_id = (request.POST.get('proveedor_id') or '').strip()
            fecha_orden = _parse_iso_date((request.POST.get('fecha_orden') or '').strip())
            fecha_prometida = _parse_iso_date((request.POST.get('fecha_prometida') or '').strip())
            tiempo_entrega = int((request.POST.get('tiempo_entrega_estimado_dias') or '0').strip() or '0')
            condiciones_pago = (request.POST.get('condiciones_pago') or '').strip()
            observaciones = (request.POST.get('observaciones') or '').strip()
            requerimiento = (
                requerimientos_pendientes_qs
                .filter(id=requerimiento_id)
                .first()
                if requerimiento_id else None
            )
            proveedor = Proveedor.objects.filter(id=proveedor_id, activo=True).prefetch_related('materiales').first() if proveedor_id else None

            if not requerimiento or not proveedor or not fecha_orden:
                messages.error(request, 'Selecciona requerimiento, proveedor y fecha de orden válidos.')
                return redirect('finanzas_ordenes_compra')

            if requerimiento.ordenes_compra_generadas.filter(creada_desde_mfg=True).exists():
                messages.error(request, f'El requerimiento {requerimiento.folio} ya tiene una orden de compra generada.')
                return redirect('finanzas_ordenes_compra')

            materiales_permitidos = {material.id: material for material in proveedor.materiales.filter(activo=True)}
            price_map = {
                precio.material_id: precio.precio_unitario
                for precio in ProveedorMaterialPrecio.objects.filter(proveedor=proveedor)
            }
            lineas = []
            for detalle in requerimiento.detalles.all():
                if detalle.material_id not in materiales_permitidos:
                    messages.error(request, f'El proveedor {proveedor.nombre} no tiene asignado el material {detalle.material.sku}.')
                    return redirect('finanzas_ordenes_compra')
                cantidad_pedida = detalle.cantidad_solicitada or detalle.cantidad_sugerida_compra or detalle.cantidad_faltante
                precio_unitario = _parse_decimal_text(price_map.get(detalle.material_id, '0'), Decimal('0'))
                subtotal = (cantidad_pedida * precio_unitario).quantize(Decimal('0.01'))
                lineas.append({
                    'material': detalle.material,
                    'sku': detalle.material.sku,
                    'descripcion': detalle.material.nombre,
                    'um': detalle.material.um,
                    'cantidad_pedida': cantidad_pedida,
                    'precio_unitario': precio_unitario,
                    'subtotal': subtotal,
                })

            total_estimado = sum((linea['subtotal'] for linea in lineas), Decimal('0'))
            with transaction.atomic():
                folio = _next_orden_compra_folio()
                while OrdenCompra.objects.filter(folio=folio).exists():
                    folio = _next_orden_compra_folio()
                orden = OrdenCompra.objects.create(
                    folio=folio,
                    proveedor=proveedor,
                    requerimiento_origen=requerimiento,
                    fecha_orden=fecha_orden,
                    fecha_prometida=fecha_prometida,
                    tiempo_entrega_estimado_dias=tiempo_entrega,
                    condiciones_pago=condiciones_pago,
                    observaciones=f'Requerimiento origen: {requerimiento.folio}\n{observaciones}'.strip(),
                    estado=OrdenCompra.EstadoOrden.APROBADA,
                    creada_desde_mfg=True,
                    total_estimado=total_estimado,
                    creado_por=request.user,
                )
                for linea in lineas:
                    OrdenCompraDetalle.objects.create(orden=orden, **linea)

            messages.success(request, f'Orden de compra {orden.folio} generada desde {requerimiento.folio}.')
            return redirect('finanzas_ordenes_compra')

    requerimientos_pendientes = list(requerimientos_pendientes_qs[:20])
    ordenes = list(
        OrdenCompra.objects
        .filter(creada_desde_mfg=True)
        .select_related('proveedor', 'requerimiento_origen', 'requerimiento_origen__bom', 'creado_por')
        .prefetch_related('detalles')
        .order_by('-fecha_creacion')[:20]
    )
    return render(request, 'finanzas/ordenes_compra.html', {
        'requerimientos_pendientes': requerimientos_pendientes,
        'ordenes': ordenes,
        'proveedores_catalogo': Proveedor.objects.filter(activo=True).order_by('nombre'),
        'opciones_condiciones_pago': ORDEN_CONDICIONES_PAGO,
        'estado_enviada': OrdenCompra.EstadoOrden.ENVIADA,
        'estado_parcial': OrdenCompra.EstadoOrden.PARCIAL,
        'estado_borrador': OrdenCompra.EstadoOrden.BORRADOR,
        'estado_aprobada': OrdenCompra.EstadoOrden.APROBADA,
    })


@login_required(login_url='login')
@never_cache
def control_recursos_mfg(request):
    if not _usuario_puede_control_recursos_mfg(request.user):
        messages.error(request, 'No tienes permisos para administrar el control de recursos MFG.')
        return redirect('home')

    ordenes_catalogo = list(
        OrdenFabricacion.objects
        .select_related('bom')
        .order_by('-fecha_actualizacion')[:30]
    )
    operadores_catalogo = list(
        UsuarioERP.objects
        .filter(activo=True)
        .select_related('departamento')
        .order_by('first_name', 'last_name', 'username')
    )

    if request.method == 'POST':
        accion = (request.POST.get('resource_action') or '').strip()

        if accion == 'registrar_maquina':
            linea = (request.POST.get('linea_produccion') or '').strip()
            maquina = (request.POST.get('maquina_nombre') or '').strip()
            costo_hora = _parse_decimal_text((request.POST.get('costo_hora') or '').strip(), Decimal('0'))
            notas = (request.POST.get('notas_maquina') or '').strip()
            activo = request.POST.get('activo_maquina') == 'on'

            if not linea or not maquina or costo_hora <= 0:
                messages.error(request, 'Línea, máquina y costo hora válido son obligatorios.')
                return redirect('control_recursos_mfg')

            costo_obj, created = CostoHoraMaquina.objects.get_or_create(
                linea_produccion=linea,
                maquina_nombre=maquina,
                defaults={
                    'costo_hora': costo_hora,
                    'activo': activo,
                    'notas': notas,
                    'registrado_por': request.user,
                    'actualizado_por': request.user,
                },
            )
            if not created:
                costo_obj.costo_hora = costo_hora
                costo_obj.activo = activo
                costo_obj.notas = notas
                costo_obj.actualizado_por = request.user
                costo_obj.save(update_fields=['costo_hora', 'activo', 'notas', 'actualizado_por', 'fecha_actualizacion'])

            _mark_dashboard_sync(request, scope='produccion')
            messages.success(request, f'Costo hora de máquina {maquina} registrado correctamente.')
            return redirect('control_recursos_mfg')

        if accion == 'registrar_operador':
            operador_id = (request.POST.get('operador_id') or '').strip()
            nomina_hora = _parse_decimal_text((request.POST.get('nomina_hora') or '').strip(), Decimal('0'))
            asistencia = _parse_decimal_text((request.POST.get('porcentaje_asistencia') or '').strip(), Decimal('100'))
            desempeno = _parse_decimal_text((request.POST.get('factor_desempeno') or '').strip(), Decimal('100'))
            notas = (request.POST.get('notas_operador') or '').strip()
            activo = request.POST.get('activo_operador') == 'on'
            operador = UsuarioERP.objects.filter(id=operador_id, activo=True).first() if operador_id else None

            if not operador or nomina_hora <= 0:
                messages.error(request, 'Selecciona un operador válido y captura una nómina por hora mayor a cero.')
                return redirect('control_recursos_mfg')

            costo_operador, created = CostoHoraOperador.objects.get_or_create(
                operador=operador,
                defaults={
                    'nomina_hora': nomina_hora,
                    'porcentaje_asistencia': asistencia,
                    'factor_desempeno': desempeno,
                    'activo': activo,
                    'notas': notas,
                    'registrado_por': request.user,
                    'actualizado_por': request.user,
                },
            )
            if not created:
                costo_operador.nomina_hora = nomina_hora
                costo_operador.porcentaje_asistencia = asistencia
                costo_operador.factor_desempeno = desempeno
                costo_operador.activo = activo
                costo_operador.notas = notas
                costo_operador.actualizado_por = request.user
                costo_operador.save()

            _mark_dashboard_sync(request, scope='produccion')
            messages.success(request, f'Costo hora del operador {operador.get_full_name() or operador.username} registrado correctamente.')
            return redirect('control_recursos_mfg')

        if accion == 'registrar_uso':
            orden_id = (request.POST.get('orden_id') or '').strip()
            tipo_recurso = (request.POST.get('tipo_recurso') or '').strip()
            maquina_id = (request.POST.get('costo_maquina_id') or '').strip()
            operador_cost_id = (request.POST.get('costo_operador_id') or '').strip()
            horas_reales = _parse_decimal_text((request.POST.get('horas_reales') or '').strip(), Decimal('0'))
            notas = (request.POST.get('notas_uso') or '').strip()

            orden = OrdenFabricacion.objects.filter(id=orden_id).first() if orden_id else None
            costo_maquina = CostoHoraMaquina.objects.filter(id=maquina_id, activo=True).first() if maquina_id else None
            costo_operador = CostoHoraOperador.objects.filter(id=operador_cost_id, activo=True).first() if operador_cost_id else None

            if not orden or horas_reales <= 0:
                messages.error(request, 'Selecciona una orden válida y captura horas reales mayores a cero.')
                return redirect('control_recursos_mfg')

            if tipo_recurso == RegistroUsoRecursoProduccion.TipoRecurso.MAQUINA and not costo_maquina:
                messages.error(request, 'Selecciona la máquina válida para registrar el uso.')
                return redirect('control_recursos_mfg')

            if tipo_recurso == RegistroUsoRecursoProduccion.TipoRecurso.OPERADOR and not costo_operador:
                messages.error(request, 'Selecciona el operador válido para registrar el uso.')
                return redirect('control_recursos_mfg')

            RegistroUsoRecursoProduccion.objects.create(
                orden=orden,
                tipo_recurso=tipo_recurso,
                costo_maquina=costo_maquina,
                costo_operador=costo_operador,
                horas_reales=horas_reales,
                notas=notas,
                registrado_por=request.user,
                actualizado_por=request.user,
            )

            _mark_dashboard_sync(request, scope='produccion')
            messages.success(request, f'Uso de recurso registrado para la orden {orden.folio}.')
            return redirect('control_recursos_mfg')

    costos_maquina = list(
        CostoHoraMaquina.objects
        .select_related('registrado_por', 'actualizado_por')
        .order_by('linea_produccion', 'maquina_nombre')
    )
    costos_operador = list(
        CostoHoraOperador.objects
        .select_related('operador', 'registrado_por', 'actualizado_por')
        .order_by('operador__username')
    )
    usos_recursos = list(
        RegistroUsoRecursoProduccion.objects
        .select_related('orden', 'costo_maquina', 'costo_operador', 'registrado_por')
        .order_by('-fecha_creacion')[:20]
    )

    return render(
        request,
        'produccion/control_recursos_mfg.html',
        {
            'lineas_produccion': ['Línea SMT-01', 'Línea SMT-02', 'Línea SMT-03'],
            'ordenes_catalogo': ordenes_catalogo,
            'operadores_catalogo': operadores_catalogo,
            'costos_maquina': costos_maquina,
            'costos_operador': costos_operador,
            'usos_recursos': usos_recursos,
            'tipo_recurso_maquina': RegistroUsoRecursoProduccion.TipoRecurso.MAQUINA,
            'tipo_recurso_operador': RegistroUsoRecursoProduccion.TipoRecurso.OPERADOR,
        },
    )


def logout_usuario(request):
    if request.user.is_authenticated:
        _registrar_acceso(request, request.user.username, True, usuario=request.user, accion='logout')
    logout(request)
    return redirect('login')


@login_required(login_url='login')
def perfil_usuario(request):
    if request.method == 'POST':
        titulo = (request.POST.get('titulo') or '').strip()
        descripcion = (request.POST.get('descripcion') or '').strip()
        prioridad = (request.POST.get('prioridad') or TicketSoporte.Prioridad.MEDIA).strip()
        prioridades_validas = {choice[0] for choice in TicketSoporte.Prioridad.choices}

        if not titulo or not descripcion:
            messages.error(request, 'Captura el título y la descripción de la solicitud.')
        elif prioridad not in prioridades_validas:
            messages.error(request, 'Selecciona una prioridad válida.')
        else:
            ticket = TicketSoporte.objects.create(
                folio=_next_ticket_folio(),
                solicitado_por=request.user,
                titulo=titulo,
                descripcion=descripcion,
                prioridad=prioridad,
            )
            messages.success(request, f'Solicitud de soporte {ticket.folio} creada correctamente.')
            return redirect('perfil')

    tickets = TicketSoporte.objects.filter(solicitado_por=request.user).order_by('-fecha_actualizacion')[:8]
    return render(
        request,
        'authentication/perfil.html',
        {
            'usuario': request.user,
            'tickets_soporte': tickets,
            'prioridades_soporte': TicketSoporte.Prioridad.choices,
        },
    )


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
            _mark_dashboard_sync(request, scope='inventario')
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

        _mark_dashboard_sync(request, scope='inventario')
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

        _mark_dashboard_sync(request, scope='inventario')
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
    materiales_catalogo = list(Material.objects.filter(activo=True).order_by('nombre', 'sku'))
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
    recepciones_pendientes = list(
        RecepcionMaterial.objects
        .filter(estado=RecepcionMaterial.EstadoRecepcion.ENVIADA)
        .select_related('creado_por', 'proveedor_registrado')
        .prefetch_related('detalles__material')
        .order_by('-fecha_recepcion', '-id')[:20]
    )
    recepciones_revisadas = list(
        RecepcionMaterial.objects
        .filter(chk_calidad=True)
        .select_related('creado_por', 'proveedor_registrado')
        .prefetch_related('detalles__material')
        .order_by('-fecha_recepcion', '-id')[:10]
    )

    if request.method == 'POST':
        accion = (request.POST.get('accion') or '').strip()
        recepcion_id = (request.POST.get('recepcion_id') or '').strip()
        resultado = (request.POST.get('resultado') or '').strip()
        observaciones = (request.POST.get('observaciones') or '').strip()

        recepcion = (
            RecepcionMaterial.objects
            .filter(id=recepcion_id, estado=RecepcionMaterial.EstadoRecepcion.ENVIADA)
            .prefetch_related('detalles')
            .first()
            if recepcion_id else None
        )

        if not recepcion:
            messages.error(request, 'Selecciona una recepción pendiente válida para inspeccionar.')
            return redirect('qa_sqa')

        accion_recomendada = RecepcionMaterial.AccionRecomendada.ACEPTAR_PARCIAL
        detalle_mensaje = f'Recepción {recepcion.id}'

        if accion == 'liberar' or resultado == 'aprobado':
            accion_recomendada = RecepcionMaterial.AccionRecomendada.ACEPTAR_TODO
            detalle_mensaje = f'Recepción {recepcion.id} aprobada por SQA y liberada para producción.'
        elif accion == 'rechazar' or resultado == 'rechazado':
            accion_recomendada = RecepcionMaterial.AccionRecomendada.CUARENTENA
            detalle_mensaje = f'Recepción {recepcion.id} rechazada por SQA y enviada a cuarentena.'
        elif resultado == 'aprobado_condicional':
            accion_recomendada = RecepcionMaterial.AccionRecomendada.ACEPTAR_PARCIAL
            detalle_mensaje = f'Recepción {recepcion.id} aprobada condicionalmente por SQA.'
        else:
            detalle_mensaje = f'Inspección SQA guardada para la recepción {recepcion.id}.'

        recepcion.chk_calidad = True
        recepcion.accion_recomendada = accion_recomendada
        if observaciones:
            recepcion.observaciones = (
                f"{recepcion.observaciones}\n\nSQA: {observaciones}".strip()
                if recepcion.observaciones else f'SQA: {observaciones}'
            )
        recepcion.save(update_fields=['chk_calidad', 'accion_recomendada', 'observaciones'])
        _mark_dashboard_sync(request, scope='qa')

        if accion_recomendada == RecepcionMaterial.AccionRecomendada.CUARENTENA:
            messages.error(request, detalle_mensaje)
        else:
            messages.success(request, detalle_mensaje)

        return redirect(f"{reverse('qa_sqa')}?dashboard_sync=1")

    return render(
        request,
        'qa/sqa.html',
        {
            'recepciones_pendientes': recepciones_pendientes,
            'recepciones_revisadas': recepciones_revisadas,
        },
    )


@login_required(login_url='login')
@never_cache
def it_clientes_compra(request):
    if not _usuario_puede_administrar_clientes(request.user):
        messages.error(request, 'No tienes permisos para administrar el catalogo de clientes de compra.')
        return redirect('home')

    clientes = ClienteCompra.objects.order_by('nombre')

    if request.method == 'POST':
        codigo = (request.POST.get('codigo') or '').strip().upper()
        nombre = (request.POST.get('nombre') or '').strip()
        contacto = (request.POST.get('contacto') or '').strip()
        email = (request.POST.get('email') or '').strip()
        telefono = (request.POST.get('telefono') or '').strip()
        activo = request.POST.get('activo') == 'on'

        if not codigo or not nombre:
            messages.error(request, 'Codigo y nombre del cliente son obligatorios.')
            return render(
                request,
                'it/clientes_compra.html',
                {
                    'clientes': clientes,
                    'form_data': {
                        'codigo': codigo,
                        'nombre': nombre,
                        'contacto': contacto,
                        'email': email,
                        'telefono': telefono,
                        'activo': activo,
                    },
                },
            )

        cliente, created = ClienteCompra.objects.update_or_create(
            codigo=codigo,
            defaults={
                'nombre': nombre,
                'contacto': contacto,
                'email': email,
                'telefono': telefono,
                'activo': activo,
            },
        )

        _mark_dashboard_sync(request, scope='qa')

        if created:
            messages.success(request, f'Cliente {cliente.nombre} registrado correctamente.')
        else:
            messages.success(request, f'Cliente {cliente.nombre} actualizado correctamente.')

        return redirect('it_clientes_compra')

    return render(
        request,
        'it/clientes_compra.html',
        {
            'clientes': clientes,
        },
    )


@login_required(login_url='login')
@never_cache
def it_usuarios(request):
    if not _usuario_puede_crear_usuarios(request.user):
        messages.error(request, 'No tienes permisos para administrar usuarios.')
        return redirect('home')

    departamentos = Departamento.objects.filter(activo=True).order_by('nombre')

    if request.method == 'POST':
        action = (request.POST.get('action') or '').strip()
        usuario_id = (request.POST.get('usuario_id') or '').strip()
        usuario_obj = User.objects.filter(id=usuario_id).select_related('departamento').first() if usuario_id else None

        if not usuario_obj:
            messages.error(request, 'Selecciona un usuario válido.')
            return redirect('it_usuarios')

        if usuario_obj.is_superuser and not request.user.is_superuser:
            messages.error(request, 'Solo un superusuario puede modificar otro superusuario.')
            return redirect('it_usuarios')

        if action == 'cambiar_departamento':
            departamento_id = (request.POST.get('departamento') or '').strip()
            departamento_obj = Departamento.objects.filter(id=departamento_id, activo=True).first() if departamento_id else None
            departamento_anterior = usuario_obj.departamento.nombre if usuario_obj.departamento else 'Sin departamento'
            usuario_obj.departamento = departamento_obj
            usuario_obj.save(update_fields=['departamento'])
            _registrar_cambio_usuario(
                usuario_obj,
                request.user,
                'Cambio de departamento',
                f'De {departamento_anterior} a {departamento_obj.nombre if departamento_obj else "Sin departamento"}.',
            )
            messages.success(request, f'Departamento actualizado para {usuario_obj.username}.')

        elif action == 'bloquear':
            if usuario_obj.id == request.user.id:
                messages.error(request, 'No puedes bloquear tu propio usuario.')
            else:
                usuario_obj.is_active = False
                usuario_obj.activo = False
                usuario_obj.save(update_fields=['is_active', 'activo'])
                _registrar_cambio_usuario(usuario_obj, request.user, 'Bloqueo de usuario', 'Usuario bloqueado desde IT.')
                messages.success(request, f'Usuario {usuario_obj.username} bloqueado.')

        elif action == 'desbloquear':
            usuario_obj.is_active = True
            usuario_obj.activo = True
            usuario_obj.save(update_fields=['is_active', 'activo'])
            _registrar_cambio_usuario(usuario_obj, request.user, 'Desbloqueo de usuario', 'Usuario activado desde IT.')
            messages.success(request, f'Usuario {usuario_obj.username} desbloqueado.')

        elif action == 'restablecer_password':
            password = request.POST.get('password') or ''
            confirm_password = request.POST.get('confirm_password') or ''
            if len(password) < 8:
                messages.error(request, 'La nueva contraseña debe tener al menos 8 caracteres.')
            elif password != confirm_password:
                messages.error(request, 'Las contraseñas no coinciden.')
            else:
                usuario_obj.set_password(password)
                usuario_obj.save(update_fields=['password'])
                _registrar_cambio_usuario(usuario_obj, request.user, 'Restablecimiento de contraseña', 'Contraseña restablecida desde IT.')
                messages.success(request, f'Contraseña restablecida para {usuario_obj.username}.')

        elif action == 'eliminar':
            if usuario_obj.id == request.user.id:
                messages.error(request, 'No puedes eliminar tu propio usuario.')
            else:
                username = usuario_obj.username
                try:
                    _registrar_cambio_usuario(usuario_obj, request.user, 'Eliminación de usuario', f'Usuario {username} eliminado desde IT.')
                    usuario_obj.delete()
                    messages.success(request, f'Usuario {username} eliminado.')
                except ProtectedError:
                    messages.error(request, f'No se puede eliminar {username} porque tiene registros asociados. Puedes bloquearlo.')
                except IntegrityError:
                    messages.error(request, f'No se puede eliminar {username} porque tiene registros asociados. Puedes bloquearlo.')

        else:
            messages.error(request, 'Acción no reconocida.')

        return redirect('it_usuarios')

    usuarios = (
        User.objects
        .select_related('departamento')
        .order_by('-is_superuser', '-is_staff', 'username')
    )

    return render(
        request,
        'it/usuarios.html',
        {
            'usuarios': usuarios,
            'departamentos': departamentos,
        },
    )


@login_required(login_url='login')
@never_cache
def it_bitacora(request):
    if not _usuario_puede_crear_usuarios(request.user):
        messages.error(request, 'No tienes permisos para consultar bitácoras de IT.')
        return redirect('home')

    accesos = (
        BitacoraAcceso.objects
        .select_related('usuario')
        .order_by('-fecha')[:200]
    )
    cambios = (
        HistorialCambioUsuario.objects
        .select_related('usuario_afectado', 'realizado_por')
        .order_by('-fecha')[:200]
    )

    return render(
        request,
        'it/bitacora.html',
        {
            'accesos': accesos,
            'cambios': cambios,
        },
    )


@login_required(login_url='login')
@never_cache
def it_soporte(request):
    if not _usuario_puede_crear_usuarios(request.user):
        messages.error(request, 'No tienes permisos para administrar solicitudes de soporte.')
        return redirect('home')

    if request.method == 'POST':
        ticket_id = (request.POST.get('ticket_id') or '').strip()
        ticket = TicketSoporte.objects.filter(id=ticket_id).select_related('solicitado_por').first() if ticket_id else None

        if not ticket:
            messages.error(request, 'Selecciona una solicitud válida.')
            return redirect('it_soporte')

        estado = (request.POST.get('estado') or ticket.estado).strip()
        prioridad = (request.POST.get('prioridad') or ticket.prioridad).strip()
        respuesta = (request.POST.get('respuesta') or '').strip()
        estados_validos = {choice[0] for choice in TicketSoporte.Estado.choices}
        prioridades_validas = {choice[0] for choice in TicketSoporte.Prioridad.choices}

        if estado not in estados_validos or prioridad not in prioridades_validas:
            messages.error(request, 'Selecciona un estado y prioridad válidos.')
            return redirect('it_soporte')

        ticket.estado = estado
        ticket.prioridad = prioridad
        ticket.respuesta = respuesta
        ticket.asignado_a = request.user
        ticket.save(update_fields=['estado', 'prioridad', 'respuesta', 'asignado_a', 'fecha_actualizacion'])
        messages.success(request, f'Solicitud {ticket.folio} actualizada correctamente.')
        return redirect('it_soporte')

    tickets = (
        TicketSoporte.objects
        .select_related('solicitado_por', 'asignado_a')
        .order_by(
            Case(
                When(estado=TicketSoporte.Estado.NUEVO, then=Value(0)),
                When(estado=TicketSoporte.Estado.EN_PROCESO, then=Value(1)),
                default=Value(2),
            ),
            '-fecha_actualizacion',
        )[:200]
    )

    return render(
        request,
        'it/soporte.html',
        {
            'tickets': tickets,
            'estados_soporte': TicketSoporte.Estado.choices,
            'prioridades_soporte': TicketSoporte.Prioridad.choices,
        },
    )


@login_required(login_url='login')
@never_cache
def qa_oqa(request):
    clientes_compra = list(ClienteCompra.objects.filter(activo=True).order_by('nombre'))
    lotes_pendientes = list(
        LoteProduccion.objects
        .filter(estado=LoteProduccion.EstadoLote.CAPTURADO)
        .select_related('bom', 'orden_fabricacion', 'creado_por', 'cliente_destino')
        .order_by('-fecha_captura', '-hora_captura', '-id')[:20]
    )
    lotes_revisados = list(
        LoteProduccion.objects
        .filter(estado__in=[
            LoteProduccion.EstadoLote.VALIDADO,
            LoteProduccion.EstadoLote.RECHAZADO,
        ])
        .select_related('bom', 'orden_fabricacion', 'creado_por', 'cliente_destino')
        .order_by('-fecha_actualizacion', '-id')[:12]
    )

    if request.method == 'POST':
        lote_id = (request.POST.get('lote_id') or '').strip()
        cliente_destino_id = (request.POST.get('cliente_destino') or '').strip()
        decision = (request.POST.get('decision_final') or '').strip()
        observaciones = (request.POST.get('observaciones_oqa') or '').strip()

        lote = (
            LoteProduccion.objects.filter(id=lote_id).select_related('bom', 'orden_fabricacion').first()
            if lote_id else None
        )

        if not lote:
            messages.error(request, 'Selecciona un lote válido para evaluación OQA.')
            return redirect('qa_oqa')

        cliente_destino = ClienteCompra.objects.filter(id=cliente_destino_id, activo=True).first() if cliente_destino_id else None
        if not cliente_destino:
            messages.error(request, 'Selecciona un cliente destino válido del catalogo.')
            return render(
                request,
                'qa/oqa.html',
                {
                    'lotes_pendientes': lotes_pendientes,
                    'lotes_revisados': lotes_revisados,
                    'clientes_compra': clientes_compra,
                },
            )

        if decision == 'liberado':
            lote.estado = LoteProduccion.EstadoLote.VALIDADO
            mensaje = f'Lote terminado {lote.folio} liberado por OQA para embarque.'
        elif decision in ['retenido', 'bloqueado']:
            lote.estado = LoteProduccion.EstadoLote.RECHAZADO
            mensaje = f'Lote terminado {lote.folio} retenido por OQA para retrabajo.'
        else:
            mensaje = f'Evaluación OQA guardada para el lote {lote.folio}.'

        if observaciones:
            lote.observaciones = (
                f"{lote.observaciones}\n\nOQA: {observaciones}".strip()
                if lote.observaciones else f'OQA: {observaciones}'
            )
        lote.cliente_destino = cliente_destino
        lote.save(update_fields=['estado', 'cliente_destino', 'observaciones', 'fecha_actualizacion'])
        _mark_dashboard_sync(request, scope='qa')

        if lote.estado == LoteProduccion.EstadoLote.RECHAZADO:
            messages.error(request, mensaje)
        else:
            messages.success(request, mensaje)

        return redirect(f"{reverse('qa_oqa')}?dashboard_sync=1")

    return render(
        request,
        'qa/oqa.html',
        {
            'clientes_compra': clientes_compra,
            'lotes_pendientes': lotes_pendientes,
            'lotes_revisados': lotes_revisados,
        },
    )


@login_required(login_url='login')
@never_cache
def qa_customer_service(request):
    clientes_compra = list(ClienteCompra.objects.filter(activo=True).order_by('nombre'))
    reclamos_abiertos = list(
        ReclamoCliente.objects
        .exclude(estado_reclamo=ReclamoCliente.EstadoReclamo.CERRADO)
        .select_related('creado_por', 'cliente_compra')
        .order_by('-fecha_actualizacion', '-id')[:15]
    )
    reclamos_recientes = list(
        ReclamoCliente.objects
        .select_related('creado_por', 'cliente_compra')
        .order_by('-fecha_actualizacion', '-id')[:20]
    )

    if request.method == 'POST':
        folio = (request.POST.get('folio_reclamo') or '').strip()
        estado = (request.POST.get('estado_reclamo') or '').strip()
        cliente_id = (request.POST.get('cliente') or '').strip()
        producto_lote = (request.POST.get('producto_lote') or '').strip()
        tipo_reclamo = (request.POST.get('tipo_reclamo') or '').strip()
        descripcion = (request.POST.get('descripcion') or '').strip()
        prioridad = (request.POST.get('prioridad') or '').strip() or ReclamoCliente.PrioridadReclamo.MEDIA
        cliente_obj = ClienteCompra.objects.filter(id=cliente_id, activo=True).first() if cliente_id else None

        if not folio or not cliente_obj or not tipo_reclamo:
            messages.error(request, 'Folio, cliente y tipo de reclamo son obligatorios y deben seleccionarse del catalogo.')
            return render(
                request,
                'qa/customer_service.html',
                {
                    'clientes_compra': clientes_compra,
                    'reclamos_abiertos': reclamos_abiertos,
                    'reclamos_recientes': reclamos_recientes,
                },
            )

        reclamo, created = ReclamoCliente.objects.update_or_create(
            folio=folio,
            defaults={
                'cliente': cliente_obj.nombre,
                'cliente_compra': cliente_obj,
                'producto_lote': producto_lote,
                'tipo_reclamo': tipo_reclamo,
                'estado_reclamo': estado or ReclamoCliente.EstadoReclamo.ABIERTO,
                'prioridad': prioridad,
                'descripcion': descripcion,
                'creado_por': request.user,
            },
        )

        if reclamo.estado_reclamo == ReclamoCliente.EstadoReclamo.CERRADO:
            messages.success(request, f'Reclamo {reclamo.folio} cerrado y comunicado al cliente.')
        elif created:
            messages.success(request, f'Reclamo {reclamo.folio} registrado en Customer Service.')
        else:
            messages.success(request, f'Reclamo {reclamo.folio} actualizado en Customer Service.')

        _mark_dashboard_sync(request, scope='qa')
        return redirect(f"{reverse('qa_customer_service')}?dashboard_sync=1")

    return render(
        request,
        'qa/customer_service.html',
        {
            'clientes_compra': clientes_compra,
            'reclamos_abiertos': reclamos_abiertos,
            'reclamos_recientes': reclamos_recientes,
        },
    )


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


def _redirect_preserving_tab(request, route_name, extra_params=None):
    from django.urls import reverse

    params = {}
    tab_value = (request.GET.get('tab') or request.POST.get('tab') or '').strip()
    if tab_value:
        params['tab'] = tab_value

    if extra_params:
        for key, value in extra_params.items():
            if value is not None and value != '':
                params[key] = value

    target = reverse(route_name)
    if params:
        return redirect(f"{target}?{urlencode(params)}")
    return redirect(target)


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
                return _redirect_preserving_tab(request, 'planificacion_produccion')

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
        return _redirect_preserving_tab(request, 'planificacion_produccion')

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

        return _redirect_preserving_tab(
            request,
            'requerimiento_materiales_produccion',
            {
                'bom': bom_obj.id,
                'cantidad': cantidad_planificada,
            }
        )

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
def plan_produccion_diario(request):
    """
    Vista para mostrar el plan de producción diario con todos los lotes a producir
    en formato similar al ejemplo proporcionado. Soporta rango de fechas.
    """
    fecha_inicio_raw = (request.GET.get('fecha_inicio') or '').strip()
    fecha_fin_raw = (request.GET.get('fecha_fin') or '').strip()
    folio_filtro = (request.GET.get('folio') or '').strip()
    bom_filtro = (request.GET.get('bom') or '').strip()
    linea_filtro = (request.GET.get('linea') or '').strip()
    
    # Establecer rango de fechas (por defecto: hoy a 7 días después)
    fecha_hoy = date.today()
    fecha_inicio = _parse_iso_date(fecha_inicio_raw) if fecha_inicio_raw else fecha_hoy
    fecha_fin = _parse_iso_date(fecha_fin_raw) if fecha_fin_raw else (fecha_hoy + timedelta(days=7))
    
    # Asegurar que fecha_inicio <= fecha_fin
    if fecha_inicio > fecha_fin:
        fecha_inicio, fecha_fin = fecha_fin, fecha_inicio
    
    # Obtener órdenes de fabricación que se solapan con el rango de fechas
    # Incluir órdenes en estados activos y completadas
    ofs_query = OrdenFabricacion.objects.filter(
        estado__in=[
            OrdenFabricacion.EstadoOF.BORRADOR,
            OrdenFabricacion.EstadoOF.EN_PROCESO,
            OrdenFabricacion.EstadoOF.PAUSADA,
            OrdenFabricacion.EstadoOF.COMPLETADA,
        ]
    ).select_related('bom', 'plan', 'creado_por').prefetch_related('detalles__material', 'lotes_produccion')
    
    # Filtrar por línea si está especificada
    if linea_filtro:
        ofs_query = ofs_query.filter(linea_produccion=linea_filtro)

    # Filtrar por folio de OF
    if folio_filtro:
        ofs_query = ofs_query.filter(folio__icontains=folio_filtro)

    # Filtrar por modelo/código BOM
    if bom_filtro:
        ofs_query = ofs_query.filter(
            Q(bom__codigo__icontains=bom_filtro) |
            Q(bom__producto__icontains=bom_filtro)
        )
    
    # Filtrar por rango de fechas programadas
    ofs_query = ofs_query.filter(
        fecha_inicio_programada__lte=fecha_fin,
        fecha_fin_programada__gte=fecha_inicio
    )
    
    # Obtener lotes de producción en el rango de fechas
    lotes_rango_query = LoteProduccion.objects.filter(
        fecha_captura__gte=fecha_inicio,
        fecha_captura__lte=fecha_fin
    ).select_related('bom', 'orden_fabricacion').order_by('fecha_captura', 'hora_captura')
    
    if linea_filtro:
        lotes_rango_query = lotes_rango_query.filter(linea_produccion=linea_filtro)

    if folio_filtro:
        lotes_rango_query = lotes_rango_query.filter(
            Q(folio__icontains=folio_filtro) |
            Q(orden_fabricacion__folio__icontains=folio_filtro)
        )

    if bom_filtro:
        lotes_rango_query = lotes_rango_query.filter(
            Q(bom__codigo__icontains=bom_filtro) |
            Q(bom__producto__icontains=bom_filtro)
        )
    
    lotes_rango = list(lotes_rango_query)
    
    # Construir datos para la tabla de producción
    plan_datos = []
    contador = 1
    
    for of in ofs_query.order_by('linea_produccion', 'folio'):
        # Calcular avance
        avance = 0
        if of.cantidad_planificada > 0:
            avance = min(int(round(float(of.cantidad_producida / of.cantidad_planificada) * 100)), 100)
        
        # Obtener TODOS los lotes de esta OF (no solo los del rango)
        lotes_of = list(of.lotes_produccion.all())
        
        # Sumar cantidades de lotes
        cantidad_en_lotes = sum(lote.cantidad_producida for lote in lotes_of)

        ultima_fecha_produccion = None
        if lotes_of:
            ultima_fecha_produccion = max(lote.fecha_captura for lote in lotes_of if lote.fecha_captura)
        
        # Resto a producir
        cantidad_pendiente = max(of.cantidad_planificada - cantidad_en_lotes, Decimal('0'))
        
        # Detalles de materiales
        detalles = []
        for det in of.detalles.all():
            detalles.append({
                'sku': det.material.sku,
                'nombre': det.material.nombre,
                'um': det.material.um,
                'cantidad_requerida': float(det.cantidad_requerida),
                'cantidad_consumida': float(det.cantidad_consumida or 0),
            })
        
        plan_datos.append({
            'num_orden': contador,
            'folio': of.folio,
            'producto': of.bom.producto,
            'codigo_bom': of.bom.codigo,
            'linea': of.linea_produccion or '-',
            'turno': of.turno or '-',
            'cantidad_planificada': float(of.cantidad_planificada),
            'cantidad_producida': float(of.cantidad_producida or 0),
            'cantidad_pendiente': float(cantidad_pendiente),
            'avance_pct': avance,
            'fecha_produccion': ultima_fecha_produccion,
            'estado': of.estado,
            'estado_label': of.get_estado_display(),
            'fecha_inicio_programada': of.fecha_inicio_programada,
            'fecha_fin_programada': of.fecha_fin_programada,
            'num_lotes': len(lotes_of),
            'lotes': [
                {
                    'folio': lote.folio,
                    'cantidad': float(lote.cantidad_producida),
                    'fecha': lote.fecha_captura,
                    'hora': lote.hora_captura,
                    'operador': lote.operador,
                    'estado': lote.estado,
                    'estado_label': lote.get_estado_display(),
                }
                for lote in sorted(lotes_of, key=lambda x: x.fecha_captura)
            ],
            'detalles': detalles,
        })
        contador += 1
    
    # Obtener líneas de producción para el filtro
    # Mantener consistencia con los catálogos vigentes del sistema (solo 3 líneas SMT).
    lineas_disponibles = [
        'Línea SMT-01',
        'Línea SMT-02',
        'Línea SMT-03',
    ]
    
    # KPIs del rango de fechas
    total_planificado = sum(p['cantidad_planificada'] for p in plan_datos)
    total_producido = sum(p['cantidad_producida'] for p in plan_datos)
    total_pendiente = sum(p['cantidad_pendiente'] for p in plan_datos)
    eficiencia_diaria = 0
    if total_planificado > 0:
        eficiencia_diaria = int(round((total_producido / total_planificado) * 100))
    
    context = {
        'fecha_inicio': fecha_inicio,
        'fecha_inicio_str': fecha_inicio.isoformat(),
        'fecha_fin': fecha_fin,
        'fecha_fin_str': fecha_fin.isoformat(),
        'rango_dias': (fecha_fin - fecha_inicio).days + 1,
        'folio_seleccionado': folio_filtro,
        'bom_seleccionado': bom_filtro,
        'linea_seleccionada': linea_filtro,
        'lineas_disponibles': lineas_disponibles,
        'plan_datos': plan_datos,
        'lotes_del_dia': lotes_rango,
        'kpis': {
            'total_ofs': len(plan_datos),
            'total_planificado': float(total_planificado),
            'total_producido': float(total_producido),
            'total_pendiente': float(total_pendiente),
            'eficiencia_diaria': eficiencia_diaria,
            'num_lotes_capturados': len(lotes_rango),
        }
    }
    
    return render(request, 'produccion/plan_produccion_diario.html', context)


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
            .prefetch_related('detalles__material', 'lotes_produccion', 'scraps_defectos__informe_qa')
            .order_by('-fecha_creacion')[:15]
        )
        data = []
        for of in ofs:
            detalles = list(of.detalles.all())
            scraps = list(of.scraps_defectos.all())
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
                'scraps': [
                    {
                        'cantidad_defectos': scrap.cantidad_defectos,
                        'tipo_defecto': scrap.get_tipo_defecto_display(),
                        'causa': scrap.causa,
                        'qa_validado': hasattr(scrap, 'informe_qa'),
                    }
                    for scrap in scraps[:6]
                ],
            })
        return data

    if request.method == 'POST' and (request.POST.get('scrap_action') or '').strip() == 'registrar_scrap':
        orden_id = (request.POST.get('scrap_orden_id') or '').strip()
        lote_id = (request.POST.get('scrap_lote_id') or '').strip()
        cantidad_defectos = _parse_decimal_text((request.POST.get('scrap_cantidad') or '').strip(), Decimal('0'))
        tipo_defecto = (request.POST.get('scrap_tipo_defecto') or '').strip()
        causa = (request.POST.get('scrap_causa') or '').strip()
        descripcion = (request.POST.get('scrap_descripcion') or '').strip()

        orden = OrdenFabricacion.objects.filter(id=orden_id).first() if orden_id else None
        lote = LoteProduccion.objects.filter(id=lote_id).select_related('orden_fabricacion').first() if lote_id else None

        if not orden and lote and lote.orden_fabricacion_id:
            orden = lote.orden_fabricacion

        if not orden and not lote:
            messages.error(request, 'Debes seleccionar una orden o un lote para registrar el scrap/defecto.')
            return redirect('ordenes_fabricacion')

        if cantidad_defectos <= 0 or not tipo_defecto or not causa:
            messages.error(request, 'Cantidad, tipo de defecto y causa son obligatorios para registrar scrap.')
            return redirect('ordenes_fabricacion')

        RegistroScrapDefecto.objects.create(
            orden=orden,
            lote=lote,
            cantidad_defectos=cantidad_defectos,
            tipo_defecto=tipo_defecto,
            causa=causa,
            descripcion=descripcion,
            registrado_por=request.user,
            actualizado_por=request.user,
        )

        _mark_dashboard_sync(request, scope='produccion')
        messages.success(request, 'Registro de scrap/defecto guardado y enviado a QA para validación.')
        return redirect('ordenes_fabricacion')

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

        lote_generado = None
        if accion_estado == OrdenFabricacion.EstadoOF.COMPLETADA:
            lote_generado = _ensure_auto_lote_for_of(of_obj, request.user)

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

        mensaje = f'Orden {of_obj.folio} actualizada a estado {of_obj.get_estado_display()}.'
        if lote_generado:
            mensaje += f' Se generó automáticamente el lote {lote_generado.folio}.'
        _mark_dashboard_sync(request, scope='produccion')
        messages.success(request, mensaje)
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
        'scraps_recientes': RegistroScrapDefecto.objects.select_related('orden', 'lote').order_by('-fecha_creacion')[:12],
        'lotes_catalogo': LoteProduccion.objects.select_related('orden_fabricacion').order_by('-fecha_actualizacion')[:20],
        'lineas_produccion': LINEAS_PRODUCCION,
        'turnos': TURNOS,
        'tipos_defecto': RegistroScrapDefecto.TipoDefecto.choices,
        'estado_en_proceso': OrdenFabricacion.EstadoOF.EN_PROCESO,
        'estado_pausada': OrdenFabricacion.EstadoOF.PAUSADA,
        'estado_completada': OrdenFabricacion.EstadoOF.COMPLETADA,
        'estado_cancelada': OrdenFabricacion.EstadoOF.CANCELADA,
        'estado_borrador': OrdenFabricacion.EstadoOF.BORRADOR,
    })


@login_required(login_url='login')
@never_cache
def qa_qqa_defectos(request):
    if not _usuario_puede_validar_defectos(request.user):
        messages.error(request, 'No tienes permisos para validar defectos en QA.')
        return redirect('home')

    if request.method == 'POST':
        defecto_id = (request.POST.get('defecto_id') or '').strip()
        resultado = (request.POST.get('resultado_validacion') or '').strip() or InformeValidacionDefectoQA.ResultadoValidacion.EN_ANALISIS
        falla_maquina = request.POST.get('falla_maquina') == 'on'
        informe = (request.POST.get('informe_qa') or '').strip()
        acciones = (request.POST.get('acciones_contencion') or '').strip()
        defecto = (
            RegistroScrapDefecto.objects
            .select_related('orden', 'lote')
            .filter(id=defecto_id)
            .first()
            if defecto_id else None
        )

        if not defecto or not informe:
            messages.error(request, 'Selecciona un defecto válido y captura el informe QA.')
            return redirect('qa_qqa_defectos')

        informe_obj, created = InformeValidacionDefectoQA.objects.update_or_create(
            defecto=defecto,
            defaults={
                'resultado_validacion': resultado,
                'falla_maquina': falla_maquina,
                'informe': informe,
                'acciones_contencion': acciones,
                'validado_por': request.user,
            },
        )

        _mark_dashboard_sync(request, scope='qa')
        messages.success(request, f'Informe QA {"creado" if created else "actualizado"} para el defecto {defecto.id}.')
        return redirect('qa_qqa_defectos')

    defectos_pendientes = list(
        RegistroScrapDefecto.objects
        .filter(informe_qa__isnull=True)
        .select_related('orden', 'lote', 'registrado_por')
        .order_by('-fecha_creacion')[:20]
    )
    informes_recientes = list(
        InformeValidacionDefectoQA.objects
        .select_related('defecto__orden', 'defecto__lote', 'validado_por')
        .order_by('-fecha_actualizacion')[:20]
    )

    return render(
        request,
        'qa/qqa_defectos.html',
        {
            'defectos_pendientes': defectos_pendientes,
            'informes_recientes': informes_recientes,
            'resultado_choices': InformeValidacionDefectoQA.ResultadoValidacion.choices,
        },
    )


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


def _ensure_auto_lote_for_of(of_obj, user):
    if not of_obj or (of_obj.cantidad_producida or Decimal('0')) <= 0:
        return None

    cantidad_lote_existente = (
        of_obj.lotes_produccion.aggregate(total=Sum('cantidad_producida')).get('total')
        or Decimal('0')
    )
    cantidad_faltante_lote = ((of_obj.cantidad_producida or Decimal('0')) - cantidad_lote_existente).quantize(Decimal('0.01'))

    if cantidad_faltante_lote <= 0:
        return None

    marca_tiempo = of_obj.fecha_fin_real or timezone.now()
    return LoteProduccion.objects.create(
        folio=_next_lote_folio(),
        bom=of_obj.bom,
        orden_fabricacion=of_obj,
        fecha_captura=marca_tiempo.date(),
        hora_captura=marca_tiempo.time().replace(microsecond=0),
        linea_produccion=of_obj.linea_produccion or '',
        turno=of_obj.turno or '',
        cantidad_producida=cantidad_faltante_lote,
        operador=(user.get_full_name() or user.username or '').strip(),
        estado=LoteProduccion.EstadoLote.CAPTURADO,
        observaciones=f'Lote autogenerado al completar la OF {of_obj.folio}.',
        creado_por=user,
    )


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

