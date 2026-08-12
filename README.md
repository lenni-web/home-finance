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

Alle fachlichen Seiten und Dokumentdownloads erfordern eine Anmeldung. Der erste
Superuser kann sich anschließend unter `/accounts/login/` anmelden. Originaldokumente
werden ausschließlich über eine authentifizierte Django-Route ausgeliefert.

## Produktionsbetrieb auf Debian (bewusst ohne HTTPS)

Voraussetzungen sind Docker Engine, das Compose-Plugin und Git. Auf dem Server:

```bash
cp .env.production.example .env
# Alle Platzhalter, Host/IP und Passwörter in .env ändern
./scripts/start-production.sh
docker compose -f compose.yaml -f compose.prod.yaml exec web \
  python manage.py createsuperuser
```

Die Anwendung läuft über Gunicorn auf `http://SERVER-IP:8000`. Ohne HTTPS darf sie nur
in einem vertrauenswürdigen lokalen Netz oder über ein VPN erreichbar sein. Sie sollte
nicht direkt ins Internet freigegeben werden. `DJANGO_SECURE_COOKIES` bleibt ohne HTTPS
auf `0`; bei einer späteren HTTPS-Einrichtung muss es auf `1` gesetzt werden.

Der Worker verarbeitet OCR und Kontoauszüge unabhängig vom Webprozess. Redis dient nur
als lokale Aufgabenwarteschlange; PostgreSQL und Dokumente liegen in eigenen Volumes.
Ein zusätzlicher Celery-Beat-Dienst stößt den fälligen E-Mail-Abruf einmal pro Minute an;
das in den Einstellungen gewählte Intervall entscheidet, ob tatsächlich abgerufen wird.

## E-Mail-Import über IMAP

Unter **Einstellungen → E-Mail-Import über IMAP** lassen sich Server, Port,
SSL/TLS beziehungsweise STARTTLS, Benutzername, Passwort, Ordner und Abrufintervall
hinterlegen. Unterstützt werden PDF-, JPEG- und PNG-Anhänge bis 30 MB. PDFs werden als
Rechnungen, Bilder als Kassenbelege angelegt und anschließend vom vorhandenen
Hintergrunddienst analysiert.

Empfohlen ist ein eigenes Postfach mit einem nur dafür vorgesehenen App-Passwort. Eine
Absenderliste kann den Import auf bekannte Adressen beschränken. Ist sie leer, werden
Anhänge aller Absender akzeptiert. Verarbeitete IMAP-UIDs und Datei-Prüfsummen verhindern
Doppelimporte. Verbindungstest und manueller Abruf stehen direkt in den Einstellungen
zur Verfügung.

Das IMAP-Passwort liegt mit Fernet verschlüsselt in PostgreSQL. Der Schlüssel wird aus
`EMAIL_CREDENTIAL_KEY` abgeleitet. Dieser Wert muss vor dem ersten Speichern gesetzt,
geheim gehalten und zusammen mit der `.env` gesichert werden. Wird er später geändert,
kann das gespeicherte Passwort nicht mehr entschlüsselt werden und muss neu eingegeben
werden.

## Backup und Wiederherstellung

Ein vollständiges Backup enthält PostgreSQL, alle Originaldokumente und eine Kopie der
aktuellen `.env`. Es erhält Dateirechte `0600` und gehört wegen der enthaltenen
Finanzdaten zusätzlich verschlüsselt beziehungsweise auf einen verschlüsselten
Datenträger:

```bash
./scripts/backup.sh
```

Standardziel ist `backups/`; ein anderes Ziel kann mit `BACKUP_DIR=/sicherer/pfad`
gesetzt werden. Wiederherstellen ersetzt Datenbank und Dokumentarchiv vollständig und
verlangt deshalb eine ausdrückliche Bestätigung:

```bash
./scripts/restore.sh --yes /absoluter/pfad/home-finance-DATUM.tar.gz
```

Die im Backup enthaltene `environment.env` wird nicht automatisch über die aktuelle
`.env` geschrieben. Prüfsummen werden vor jeder Wiederherstellung kontrolliert.

## Auswertung und Zuordnungen

Das Dashboard wertet ausschließlich bestätigte Buchungen monatsweise aus. Es zeigt
Einnahmen, Ausgaben, Differenz, den Vergleich zum Vormonat und die Verteilung nach
Kategorien. Der Arbeitsbereich „Offene Aufgaben“ bündelt ungeprüfte Dokumente,
fehlgeschlagene Verarbeitungen, Saldo-Abweichungen und Buchungen ohne Kategorie.

Kategorisierungsregeln werden nach einer einstellbaren Priorität ausgewertet. Für den
Vergleich werden Händlerbezeichnungen konservativ vereinheitlicht; die unveränderten
Originaltexte bleiben in der Buchung erhalten. Zusätzlich schlägt die Anwendung eine
Kategorie vor, wenn frühere bestätigte Buchungen desselben normalisierten Händlers
eine ausreichend eindeutige Zuordnung ergeben. Quelle und Trefferwahrscheinlichkeit
des Vorschlags werden in der Buchungsübersicht angezeigt.

## Kontoauszug lokal anonymisieren

Das Original-PDF muss für die Parserentwicklung nicht weitergegeben werden. Das
Hilfsprogramm liest es lokal und erzeugt eine datensparsame JSON-Testdatei mit
Seitenmaßen und Textpositionen. Freie Texte werden pseudonymisiert; Datumswerte und
Beträge werden durch synthetische Werte ersetzt. PDF-Metadaten, Bilder und Anhänge
werden nicht übernommen.

Innerhalb des Containers:

```bash
mkdir -p sanitized
docker compose run --rm \
  -v "/lokaler/ordner/mit/pdf:/input:ro" \
  web python tools/sanitize_statement.py \
  "/input/Kontoauszug.pdf" sanitized/ing-layout.json
```

Ohne Docker in einer Python-Umgebung mit installierten Projektabhängigkeiten:

```bash
python tools/sanitize_statement.py \
  "/lokaler/pfad/Kontoauszug.pdf" sanitized/ing-layout.json
```

Die erzeugte JSON-Datei vor dem Weitergeben immer noch einmal manuell durchsuchen.
Der Ordner `sanitized/` ist absichtlich von Git ausgeschlossen.

Die anonymisierte Struktur lässt sich anschließend lokal prüfen:

```bash
python -m tools.inspect_ing_fixture sanitized/ing-layout.json \
  --output sanitized/ing-transactions.json
```

Der ING-Parser verwendet die Lesereihenfolge des PDFs und sichert Buchungs- und
Valutadatum zusätzlich über die Position der linken Datumsspalte ab. Informationsseiten
ohne diese Struktur werden ignoriert.

## Echten ING-Auszug lokal validieren

Das folgende Kommando gibt standardmäßig nur Anzahl, Seitenverteilung und die
laufenden Nummern unvollständiger Buchungen aus:

```bash
python -m tools.validate_ing_pdf "/lokaler/pfad/Kontoauszug.pdf"
```

Optional kann eine private JSON-Kontrollliste erzeugt werden. Sie enthält echte
Buchungsdaten, erhält Dateirechte `0600` und gehört ausschließlich in `sanitized/`:

```bash
python -m tools.validate_ing_pdf "/lokaler/pfad/Kontoauszug.pdf" \
  --output sanitized/private-validation.json
```

## Kontoauszug über die Weboberfläche importieren

1. Auf der Startseite unter `Konto anlegen` einen frei gewählten Kontonamen erfassen.
2. Auf der Startseite Dokumenttyp `Kontoauszug` und das Konto auswählen.
3. Das ING-PDF hochladen; die Verarbeitung erfolgt lokal im Hintergrund.
4. Erkannte Buchungen in der Kontrolltabelle korrigieren oder zwischenspeichern.
5. Erst `Alle Buchungen bestätigen` markiert den Auszug als importiert.

Bereits hochgeladene identische Dateien werden über ihren SHA-256-Hash abgewiesen.

## Buchungen kategorisieren

Unter `/transactions/` stehen ausschließlich bestätigte Buchungen zur Verfügung.
Die Ansicht bietet Monats-, Konto-, Kategorie-, Tag-, Personen- und Textfilter sowie
Summen für Einnahmen, Ausgaben und Differenz.

Zuordnungen lassen sich entweder direkt je Tabellenzeile oder gesammelt für markierte
Buchungen speichern. Bei einer Sammelzuordnung können aus den Zahlungspartnern Regeln
angelegt werden. Automatische Regeln werden bei späteren Kontoauszügen direkt angewendet;
nicht automatische Regeln erscheinen als sichtbarer Vorschlag.

Unter `/settings/classification/` können Kategorien, Tags, Personen und Regeln angelegt
sowie aktiviert oder deaktiviert werden. Eine Deaktivierung entfernt keine bestehenden
Zuordnungen.

## Rechnungen und Belege

PDF-, JPG- und PNG-Belege werden beim Upload ausschließlich lokal verarbeitet. Digitale
PDFs werden direkt gelesen; für Scans und Fotos verwendet der Container Tesseract mit
deutscher und englischer Sprache sowie OCRmyPDF. Das Original bleibt unverändert.

Datum, Händler und Gesamtbetrag sind Vorschläge und werden vor dem Abschluss auf einer
Prüfseite angezeigt. Dort können außerdem Kategorie, Tags und Personen vergeben sowie
passende Kontobewegungen verknüpft werden. Kandidaten werden anhand eines Zeitfensters
von sieben Tagen und – sofern erkannt – des Betrags eingeschränkt.

Unter `/documents/` steht das nach Monat sortierte Archiv mit Volltextsuche, Filtern,
Vorschau und Download zur Verfügung. OCR und Kontoauszugsimport laufen über den
separaten Hintergrund-Worker. Uploads werden sofort angenommen; der Status wechselt
anschließend von `Ausstehend` über `Wird verarbeitet` zu `Prüfung erforderlich` oder
`Fehlgeschlagen`.

Die Reihenfolge von Beleg und Kontoauszug ist unerheblich: Wird ein Kontoauszug erst
später bestätigt, prüft die Anwendung alle bisher unverknüpften Belege mit erkanntem
Datum und Betrag erneut. Mögliche Treffer werden als `Prüfung erforderlich` markiert,
aber aus Sicherheitsgründen nicht ohne Bestätigung fest verknüpft.

## Saldenprüfung von Kontoauszügen

Bei ING-Auszügen werden `Alter Saldo` und der letzte `Neue Saldo` als Kontrollwerte am
Import gespeichert. Zusätzlich speichert die Anwendung die Summe aller erkannten
Buchungen und die rechnerische Differenz:

```text
Anfangssaldo + Buchungssumme - Endsaldo = Differenz
```

Der Status lautet `Ausgeglichen`, `Abweichung` oder `Nicht prüfbar`. Salden werden nie
als Einnahme oder Ausgabe behandelt. Vorhandene Importe können ohne erneuten Upload
lokal nachberechnet werden:

```bash
docker compose exec web python manage.py reconcile_statements
```
