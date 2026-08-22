from django.urls import path

from . import views

app_name = 'stats'

urlpatterns = [
    path('dashboard/<uuid:session_id>/', views.dashboard, name='dashboard'),
    path('compare/<uuid:session_a>/<uuid:session_b>/', views.compare, name='compare'),
]
