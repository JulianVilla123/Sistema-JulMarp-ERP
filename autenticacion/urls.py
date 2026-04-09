from django.urls import path
from .views import login_usuario, register_usuario

urlpatterns = [
    path('login/', login_usuario, name='login'),
    path('register/', register_usuario, name='register'),
]
