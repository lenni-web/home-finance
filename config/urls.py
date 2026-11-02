from django.conf import settings
from django.contrib import admin
from django.urls import include, path
from django.views.generic.base import RedirectView

from ledger.login_throttle import ThrottledLoginView

urlpatterns = [
    path(
        "favicon.ico",
        RedirectView.as_view(url="/static/ledger/brand/favicon.ico", permanent=True),
    ),
    path(settings.ADMIN_URL, admin.site.urls),
    path("accounts/login/", ThrottledLoginView.as_view(), name="login"),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("ledger.urls")),
]
