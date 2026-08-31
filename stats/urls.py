from django.urls import path

from . import views

app_name = 'stats'

urlpatterns = [
    path('dashboard/<uuid:session_id>/', views.dashboard, name='dashboard'),
    path('dashboard/<uuid:session_id>/person/<int:tmdb_id>/', views.person_filmography, name='person_filmography'),
    path('compare/<uuid:session_a>/<uuid:session_b>/', views.compare, name='compare'),
]
