from django.urls import path

from . import views

app_name = 'imports'

urlpatterns = [
    path('upload/', views.UploadView.as_view(), name='upload'),
    path('upload/compare/', views.CompareUploadView.as_view(), name='upload_compare'),
]
