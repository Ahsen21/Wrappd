from django.urls import path

from . import views

app_name = 'core'

urlpatterns = [
    path('', views.landing, name='landing'),
    path('on-this-day/', views.on_this_day, name='on_this_day'),
]
