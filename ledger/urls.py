from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("settings/", views.settings, name="settings"),
    path("settings/email/", views.save_email_settings, name="save_email_settings"),
    path("settings/accounts/add/", views.add_account, name="add_account"),
    path("settings/accounts/<int:pk>/edit/", views.edit_account, name="edit_account"),
    path("documents/upload/", views.upload_document, name="upload_document"),
    path("documents/", views.document_archive, name="document_archive"),
    path("documents/<int:pk>/review/", views.document_review, name="document_review"),
    path("documents/<int:pk>/file/", views.document_download, name="document_download"),
    path("statements/<int:pk>/review/", views.statement_review, name="statement_review"),
    path("transactions/", views.transaction_overview, name="transaction_overview"),
    path("tasks/", views.open_tasks, name="open_tasks"),
    path("settings/classification/", views.manage_classification, name="manage_classification"),
    path(
        "settings/classification/<str:kind>/<int:pk>/toggle/",
        views.toggle_classification,
        name="toggle_classification",
    ),
    path("settings/classification/rules/<int:pk>/edit/", views.edit_rule, name="edit_rule"),
    path(
        "settings/classification/<str:kind>/<int:pk>/edit/",
        views.edit_classification,
        name="edit_classification",
    ),
    path("health/", views.health, name="health"),
]
