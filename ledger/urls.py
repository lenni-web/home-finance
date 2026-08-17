from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("analytics/", views.analytics, name="analytics"),
    path("settings/", views.settings, name="settings"),
    path("settings/status/", views.operational_status, name="operational_status"),
    path("settings/email/", views.save_email_settings, name="save_email_settings"),
    path("settings/accounts/add/", views.add_account, name="add_account"),
    path("settings/accounts/<int:pk>/edit/", views.edit_account, name="edit_account"),
    path("documents/upload/", views.upload_document, name="upload_document"),
    path("documents/", views.document_archive, name="document_archive"),
    path("documents/<int:pk>/review/", views.document_review, name="document_review"),
    path("documents/<int:pk>/retry/", views.retry_failed_document, name="retry_failed_document"),
    path("documents/<int:pk>/delete/", views.delete_failed_document, name="delete_failed_document"),
    path("documents/<int:pk>/file/", views.document_download, name="document_download"),
    path("matches/automatic/", views.automatic_matches, name="automatic_matches"),
    path(
        "matches/automatic/<int:pk>/revoke/",
        views.revoke_automatic_match,
        name="revoke_automatic_match",
    ),
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
    path("settings/classification/rules/<int:pk>/delete/", views.delete_rule, name="delete_rule"),
    path(
        "settings/classification/<str:kind>/<int:pk>/edit/",
        views.edit_classification,
        name="edit_classification",
    ),
    path("health/", views.health, name="health"),
]
