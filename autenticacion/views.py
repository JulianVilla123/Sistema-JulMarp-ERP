from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, get_user_model
from django.contrib import messages

# Create your views here.

User = get_user_model()

def login_usuario(request):
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            return redirect('/')  # Redirigir a la raíz después del login
        else:
            messages.error(request, 'Usuario o contraseña incorrectos.')
    return render(request, 'authentication/login.html')


def register_usuario(request):
    if request.method == 'POST':
        username = request.POST.get('username')
        first_name = request.POST.get('first_name')
        last_name = request.POST.get('last_name')
        email = request.POST.get('email')
        telefono = request.POST.get('telefono')
        numero_empleado = request.POST.get('numero_empleado')
        password = request.POST.get('password')

        if User.objects.filter(username=username).exists():
            messages.error(request, 'El nombre de usuario ya está en uso.')
        elif User.objects.filter(email=email).exists():
            messages.error(request, 'El correo ya está registrado.')
        elif not password:
            messages.error(request, 'La contraseña es obligatoria.')
        else:
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
            messages.success(request, 'Registro exitoso. Ya puedes iniciar sesión.')
            return redirect('login')

    return render(request, 'authentication/register.html')
