from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path

from . import views
from .forms import LoginForm

app_name = 'accounts'

urlpatterns = [
    path('signup/', views.SignUpView.as_view(), name='signup'),
    path(
        'login/',
        LoginView.as_view(template_name='accounts/login.html', authentication_form=LoginForm),
        name='login',
    ),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('account/', views.AccountView.as_view(), name='account'),
]
