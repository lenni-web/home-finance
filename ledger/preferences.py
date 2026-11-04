"""Anwendungsweite Einstellungen, die Formulare und Auswertungen beeinflussen."""

from functools import wraps

from django.contrib import messages
from django.shortcuts import redirect

from .models import AppSettings


def people_enabled():
    return AppSettings.current().people_enabled


def requires_setting(field, message):
    """Sperrt eine Ansicht, solange die zugehörige Einstellung ausgeschaltet ist."""

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not getattr(AppSettings.current(), field):
                messages.info(request, message)
                return redirect("dashboard")
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


def drop_people_fields(form, *names):
    """Entfernt die Personenfelder aus einem Formular, solange die Zuordnung ausgeschaltet ist.

    Bestehende Zuordnungen bleiben unberührt, weil entfernte Felder beim Speichern
    nicht verarbeitet werden.
    """
    if not people_enabled():
        for name in names:
            form.fields.pop(name, None)
