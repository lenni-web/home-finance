from django.contrib import admin
from django.urls import include, path
from django.views.generic.base import RedirectView

urlpatterns = [
    path(
        "favicon.ico",
        RedirectView.as_view(url="/static/ledger/brand/favicon.ico", permanent=True),
    ),
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("ledger.urls")),
]
