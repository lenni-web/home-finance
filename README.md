# Ausgabenarchiv – Prototyp

Lokale Django-Anwendung für Kontoauszüge als PDF, Rechnungen, Kassenbelege,
Kategorien, Tags und Personenzuordnungen.

## Entwicklungsprinzip

- Git enthält Quellcode, Datenbankschema und Beispielkonfiguration.
- Django-Migrationen übertragen jede Schemaänderung reproduzierbar.
- `media/`, PostgreSQL-Daten und `.env` werden niemals eingecheckt.
- Releases erhalten Tags wie `v0.1.0`; Server-Updates erfolgen nur auf ein solches Tag.
- Dokumente und Datenbank werden gemeinsam gesichert und wiederhergestellt.

## Lokal starten

```bash
cp .env.example .env
# Passwörter und Secret in .env ändern
docker compose up --build
```

Danach liegt die Oberfläche unter `http://localhost:8000`. Für die Verwaltung:

```bash
docker compose exec web python manage.py createsuperuser
```

## Übertragung auf Debian

1. Git-Repository auf den Server klonen.
2. `.env` nur auf dem Server anlegen.
3. `docker compose up -d --build` ausführen.
4. Bei Updates zuerst Backup erstellen, dann das gewünschte Git-Tag auschecken.
5. `docker compose up -d --build`; Migrationen werden beim Start angewendet.

Produktiv kommen HTTPS, ein Reverse Proxy, regelmäßige Backups und ein separater
Hintergrundprozess für OCR hinzu.

## Nächster Entwicklungsschritt

Der Kontoauszug-Importer erhält pro Bank einen versionierten Parser. Extrahierte
Buchungen landen zunächst im Prüfstatus und werden erst nach Bestätigung übernommen.

