"""Anwendungsweite Einstellungen, die Formulare und Auswertungen beeinflussen."""

from .models import AppSettings


def people_enabled():
    return AppSettings.current().people_enabled


def drop_people_fields(form, *names):
    """Entfernt die Personenfelder aus einem Formular, solange die Zuordnung ausgeschaltet ist.

    Bestehende Zuordnungen bleiben unberührt, weil entfernte Felder beim Speichern
    nicht verarbeitet werden.
    """
    if not people_enabled():
        for name in names:
            form.fields.pop(name, None)
