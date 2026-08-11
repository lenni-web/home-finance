from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("documents/upload/", views.upload_document, name="upload_document"),
    path("statements/<int:pk>/review/", views.statement_review, name="statement_review"),
]
