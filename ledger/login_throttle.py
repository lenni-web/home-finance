import hashlib
import logging

from django.conf import settings
from django.contrib.auth.views import LoginView
from django.core.cache import cache

logger = logging.getLogger(__name__)


def _key(request, username):
    identity = f"{(username or '').strip().lower()}|{request.META.get('REMOTE_ADDR', '')}"
    return "login-failures:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _safe(operation, default=None):
    # Ein Ausfall des Zwischenspeichers darf die Anmeldung nicht blockieren.
    try:
        return operation()
    except Exception:
        logger.warning("Login-Zähler nicht erreichbar", exc_info=True)
        return default


def is_locked(request, username):
    failures = _safe(lambda: cache.get(_key(request, username), 0), 0)
    return failures >= settings.LOGIN_MAX_FAILURES


def record_failure(request, username):
    key = _key(request, username)

    def increment():
        cache.add(key, 0, settings.LOGIN_LOCKOUT_SECONDS)
        cache.incr(key)

    _safe(increment)


def clear_failures(request, username):
    _safe(lambda: cache.delete(_key(request, username)))


class ThrottledLoginView(LoginView):
    """Login mit zeitlich begrenzter Sperre nach wiederholten Fehlversuchen."""

    def post(self, request, *args, **kwargs):
        username = request.POST.get("username", "")
        if is_locked(request, username):
            form = self.get_form()
            form.is_valid()
            form.add_error(None, "Zu viele Fehlversuche. Bitte später erneut versuchen.")
            response = self.render_to_response(self.get_context_data(form=form))
            response.status_code = 429
            return response
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        clear_failures(self.request, form.cleaned_data.get("username"))
        return super().form_valid(form)

    def form_invalid(self, form):
        record_failure(self.request, self.request.POST.get("username", ""))
        return super().form_invalid(form)
