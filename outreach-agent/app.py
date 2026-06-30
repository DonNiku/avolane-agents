"""
app.py

FastAPI-Web-Interface für den bestehenden Outreach-Agenten.

Diese Etappe baut NUR das Grundgerüst (Etappe A):
  - Eingabemaske (Schlagwort, Stadt, Anzahl Leads, Versand-Modus)
  - Lauf im Hintergrund mit Live-Fortschrittsanzeige (/status-Polling)
  - Ergebnisanzeige pro Lead als Karten, nach Score sortiert

NOCH KEIN E-Mail-Versand: Das Modus-Dropdown wird zwar angezeigt und der
gewählte Modus im Job gespeichert, löst aber noch keinen Versand aus.

outreach_agent.py wird NICHT verändert – nur importiert.

Start:
  uvicorn app:app --reload
  oder direkt:  python app.py
"""

import os
import uuid
import sqlite3
import smtplib
import threading
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr

import uvicorn
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

# Einzelschritte aus dem bestehenden Agenten importieren – so können wir den
# Fortschritt Lead für Lead anzeigen, statt nur run_agent() blind aufzurufen.
# Beim Import von outreach_agent wird auch load_dotenv() ausgeführt, die .env
# ist damit bereits geladen.
from outreach_agent import (
    search_leads,
    scrape_website,
    detect_website_features,
    score_lead,
    generate_message,
)


# ---------------------------------------------------------------------------
# SMTP-/E-Mail-Konfiguration (aus .env)
# ---------------------------------------------------------------------------

EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER")
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = os.getenv("SMTP_PORT")

# Feste Signatur für den Direktversand an Prospects. Enthält Kontaktdaten und
# einen einfachen Opt-out-Hinweis und wird an jede direkt versendete Mail
# angehängt.
SIGNATURE = """

--
Patrick
0176 82447546
hallo@avolane.de

Wenn Sie keine weitere Nachricht von mir möchten, geben Sie mir einfach kurz Bescheid – dann melde ich mich nicht wieder."""


# ---------------------------------------------------------------------------
# App- und Job-Verwaltung
# ---------------------------------------------------------------------------

app = FastAPI(title="Avolane Outreach Agent")

# Einfache In-Memory-Job-Verwaltung: job_id -> Status-Dict.
# Für ein lokales Tool völlig ausreichend (keine Persistenz nötig).
JOBS = {}

# Lock, damit gleichzeitige Lese-/Schreibzugriffe auf JOBS konsistent bleiben.
JOBS_LOCK = threading.Lock()


class RunRequest(BaseModel):
    """Eingabe-Payload für POST /run."""
    keyword: str
    city: str
    max_leads: int = 5
    mode: str = "draft"  # "draft" | "report" | "direct"


class SendRequest(BaseModel):
    """Eingabe-Payload für POST /send_single (Direktversand an einen Prospect)."""
    email: str
    message: str
    # Optionale Verlaufs-Metadaten. is_test=True (Test-Empfänger-Feld war gefüllt)
    # verhindert, dass der Versand im echten Verlauf landet.
    is_test: bool = False
    recipient_name: str = ""
    keyword: str = ""
    city: str = ""


# ---------------------------------------------------------------------------
# Versand-Verlauf (SQLite)
# ---------------------------------------------------------------------------

# Datenbankdatei im selben Ordner wie diese Datei.
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sent_history.db")


def init_db():
    """Legt die Tabelle sent_mails an, falls sie noch nicht existiert."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sent_mails (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sent_at TEXT,
                    recipient_name TEXT,
                    recipient_email TEXT,
                    message TEXT,
                    keyword TEXT,
                    city TEXT
                )
                """
            )
    except Exception as exc:
        # DB-Probleme dürfen den App-Start nicht verhindern.
        print("[DB] FEHLER bei init_db:", exc)


def log_sent_mail(recipient_name, recipient_email, message, keyword, city):
    """
    Schreibt einen Verlaufseintrag mit aktuellem ISO-Zeitstempel.

    Robust: Ein DB-Fehler wird nur geloggt und nie weitergereicht, damit der
    Versand davon nicht abbricht.
    """
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO sent_mails
                    (sent_at, recipient_name, recipient_email, message, keyword, city)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now().isoformat(),
                    recipient_name or "",
                    recipient_email or "",
                    message or "",
                    keyword or "",
                    city or "",
                ),
            )
    except Exception as exc:
        print("[DB] FEHLER bei log_sent_mail:", exc)


# Tabelle einmalig beim App-Start (Modul-Import) sicherstellen – funktioniert
# auch unter `uvicorn app:app`, nicht nur im __main__-Block.
init_db()


def _update_job(job_id, **fields):
    """Aktualisiert ein Job-Status-Dict threadsicher."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is not None:
            job.update(fields)


# ---------------------------------------------------------------------------
# Report-Versand per E-Mail (nur Modus "report")
# ---------------------------------------------------------------------------

# Lesbare Beschriftungen für die Feature-Flags (für die Report-Mail).
_FEATURE_LABELS = {
    "hat_buchungstool": "Buchungstool",
    "hat_chatbot": "Chatbot",
    "hat_kontaktformular": "Kontaktformular",
    "hat_social_media": "Social Media",
}


def _esc(value):
    """Minimal-Escaping, damit Lead-Daten das HTML der Mail nicht zerschießen."""
    return (
        str(value if value is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _build_report_html(leads, keyword, city):
    """Baut den HTML-Body der Report-Mail: pro Lead eine übersichtliche Karte."""
    cards = []
    for lead in leads:
        features = lead.get("features", {}) or {}
        # Erkannte Features als kleine Tag-Liste (nur die aktiven anzeigen).
        active = [label for key, label in _FEATURE_LABELS.items() if features.get(key)]
        feature_html = (
            " ".join(
                f'<span style="display:inline-block;background:#2A1652;color:#D8D4E8;'
                f'border:1px solid rgba(167,139,250,0.3);border-radius:8px;'
                f'padding:2px 8px;font-size:12px;margin:0 6px 6px 0;">{_esc(f)}</span>'
                for f in active
            )
            or '<span style="color:#9A8FB8;font-size:13px;">keine erkannt</span>'
        )

        # Optionale Kontaktzeilen.
        meta_rows = []
        if lead.get("address"):
            meta_rows.append(f"<div>{_esc(lead['address'])}</div>")
        if lead.get("phone"):
            meta_rows.append(f"<div>Tel.: {_esc(lead['phone'])}</div>")
        if lead.get("website"):
            meta_rows.append(f"<div>Web: {_esc(lead['website'])}</div>")
        if lead.get("email"):
            meta_rows.append(f"<div>E-Mail: {_esc(lead['email'])}</div>")
        meta_html = "".join(meta_rows)

        message_html = _esc(lead.get("message", "")).replace("\n", "<br>")

        cards.append(f"""
        <div style="background:#1F0E3D;border:1px solid rgba(167,139,250,0.18);
                    border-radius:14px;padding:18px 20px;margin-bottom:16px;">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <strong style="color:#A78BFA;font-size:17px;">{_esc(lead.get('name', 'Unbenannt'))}</strong>
            <span style="background:#7C3AED;color:#fff;border-radius:999px;
                         padding:3px 11px;font-size:13px;font-weight:bold;">
              Score {_esc(lead.get('score', 0))}/100</span>
          </div>
          <div style="color:#9A8FB8;font-size:13px;margin:6px 0;">{meta_html}</div>
          <div style="margin:8px 0;">{feature_html}</div>
          <div style="background:#130623;border:1px solid rgba(167,139,250,0.18);
                      border-radius:10px;padding:12px 14px;color:#D8D4E8;
                      font-size:14px;line-height:1.5;">{message_html}</div>
        </div>
        """)

    return f"""<!DOCTYPE html>
<html lang="de">
<body style="margin:0;padding:24px;background:#17082E;
             font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
  <div style="max-width:680px;margin:0 auto;">
    <h1 style="color:#A78BFA;font-size:22px;margin:0 0 4px;">Outreach-Report</h1>
    <p style="color:#9A8FB8;font-size:14px;margin:0 0 24px;">
      {_esc(keyword)} in {_esc(city)} – {len(leads)} Leads
    </p>
    {''.join(cards)}
  </div>
</body>
</html>"""


def send_report(leads, keyword, city):
    """
    Versendet den gesammelten Lauf als HTML-Report-Mail (Modus "report").

    Gibt "ok" bei Erfolg zurück, sonst die Fehlermeldung als String.
    Crasht nie – jeder Fehler wird abgefangen und zurückgegeben.
    """
    # Vollständigkeit der Zugangsdaten prüfen, bevor wir SMTP anfassen.
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECEIVER, SMTP_HOST, SMTP_PORT]):
        return ("SMTP-Zugangsdaten unvollständig – bitte EMAIL_SENDER, "
                "EMAIL_PASSWORD, EMAIL_RECEIVER, SMTP_HOST und SMTP_PORT in der "
                ".env setzen.")

    try:
        port = int(SMTP_PORT)
    except (TypeError, ValueError):
        return f"Ungültiger SMTP_PORT: {SMTP_PORT!r}"

    subject = f"Outreach-Report: {keyword} in {city} – {len(leads)} Leads"

    # Saubere HTML-Mail mit UTF-8 aufbauen.
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Avolane Outreach Agent", EMAIL_SENDER))
    msg["To"] = EMAIL_RECEIVER

    html_body = _build_report_html(leads, keyword, city)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    # Versand via STARTTLS (Port 587). Robust gekapselt.
    try:
        with smtplib.SMTP(SMTP_HOST, port, timeout=30) as server:
            server.starttls()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, [EMAIL_RECEIVER], msg.as_string())
        print("[Report] Mail erfolgreich an", EMAIL_RECEIVER, "gesendet")
        return "ok"
    except Exception as exc:
        print("[Report] FEHLER beim Versand:", exc)
        return f"SMTP-Fehler: {exc}"


# Fester, neutraler Betreff für den Direktversand an Prospects (nicht werblich).
DIRECT_SUBJECT = "Kurze Frage zu Ihrem Ablauf"


def send_single_mail(to_email, subject, body):
    """
    Versendet EINE einzelne Plaintext-Mail an einen Prospect (Modus "direct").

    - SMTP via Zoho (SMTP_HOST:SMTP_PORT), STARTTLS (Port 587), Login mit
      EMAIL_SENDER / EMAIL_PASSWORD. Absender und Reply-To: EMAIL_SENDER.
    - Der feste, neutrale Betreff DIRECT_SUBJECT wird verwendet (der subject-
      Parameter wird bewusst nicht für werbliche Betreffzeilen genutzt).
    - SIGNATURE wird an den body angehängt.

    Gibt "ok" bei Erfolg zurück, sonst die Fehlermeldung als String.
    Crasht nie – jeder Fehler wird abgefangen und zurückgegeben.
    """
    # Empfängeradresse muss vorhanden sein.
    if not to_email:
        return "Keine Empfänger-Adresse für diesen Lead vorhanden."

    # Vollständigkeit der Zugangsdaten prüfen, bevor wir SMTP anfassen.
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, SMTP_HOST, SMTP_PORT]):
        return ("SMTP-Zugangsdaten unvollständig – bitte EMAIL_SENDER, "
                "EMAIL_PASSWORD, SMTP_HOST und SMTP_PORT in der .env setzen.")

    try:
        port = int(SMTP_PORT)
    except (TypeError, ValueError):
        return f"Ungültiger SMTP_PORT: {SMTP_PORT!r}"

    # Signatur an den Nachrichtentext anhängen.
    full_body = (body or "") + SIGNATURE

    # Saubere Plaintext-Mail mit UTF-8 aufbauen.
    msg = MIMEMultipart("alternative")
    msg["Subject"] = DIRECT_SUBJECT
    msg["From"] = formataddr(("Patrick", EMAIL_SENDER))
    msg["To"] = to_email
    msg["Reply-To"] = EMAIL_SENDER
    msg.attach(MIMEText(full_body, "plain", "utf-8"))

    # Versand via STARTTLS (Port 587). Robust gekapselt.
    try:
        with smtplib.SMTP(SMTP_HOST, port, timeout=30) as server:
            server.starttls()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, [to_email], msg.as_string())
        print("[Direct] Mail erfolgreich an", to_email, "gesendet")
        return "ok"
    except Exception as exc:
        print("[Direct] FEHLER beim Versand:", exc)
        return f"SMTP-Fehler: {exc}"


def _run_job(job_id, keyword, city, max_leads, mode):
    """
    Hintergrund-Task: Führt den Outreach-Lauf Lead für Lead aus und
    aktualisiert dabei den Job-Status, damit die Oberfläche live mitläuft.

    Robust gekapselt: Crasht ein Lauf, wird der Job auf "error" gesetzt,
    statt die App abzureißen.
    """
    try:
        # 1. Leads suchen.
        leads = search_leads(keyword, city, max_leads)

        total = len(leads)
        _update_job(job_id, total=total)

        enriched_leads = []

        # 2. Pro Lead: scrapen, Features erkennen, scoren, Nachricht generieren.
        for index, lead in enumerate(leads, start=1):
            # Fortschritt VOR der Verarbeitung melden, damit der Name sofort
            # in der Oberfläche erscheint.
            _update_job(
                job_id,
                current=index,
                current_name=lead.get("name", ""),
            )

            # Website scrapen (gibt Dict mit text + email zurück).
            result = scrape_website(lead.get("website", ""))
            text = result["text"]
            email = result["email"]

            # Features erkennen (bekommt nur den Text).
            features = detect_website_features(text)

            # Score berechnen.
            score = score_lead(lead, features)

            # Personalisierte Nachricht generieren.
            message = generate_message(lead, features, variant_index=index - 1)

            enriched_leads.append({
                **lead,
                "email": email,
                "features": features,
                "score": score,
                "message": message,
            })

        # 3. Nach Score absteigend sortieren.
        enriched_leads.sort(key=lambda l: l["score"], reverse=True)

        # 4. Nur im Modus "report": ZUERST den Report verschicken, damit
        #    report_status im selben finalen "done"-Update enthalten ist.
        #    Sonst würde das Frontend bei "done" das Polling stoppen, bevor
        #    der Versand-Status feststeht. ("draft" verschickt nichts,
        #    "direct" bleibt vorerst funktionslos.)
        done_fields = {
            "status": "done",
            "current": total,
            "current_name": "",
            "leads": enriched_leads,
        }
        if mode == "report":
            done_fields["report_status"] = send_report(enriched_leads, keyword, city)

        # 5. EIN finales Update mit status="done" (und ggf. report_status).
        _update_job(job_id, **done_fields)

    except Exception as exc:
        # Jeder Fehler landet im Job-Status, nicht im Prozess-Crash.
        _update_job(job_id, status="error", error=str(exc))


# ---------------------------------------------------------------------------
# Endpunkte
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index():
    """Liefert die HTML-Oberfläche (Eingabemaske + Ergebnisbereich)."""
    return HTMLResponse(content=PAGE_HTML)


@app.post("/run")
def run(req: RunRequest, background_tasks: BackgroundTasks):
    """
    Startet einen Lauf im Hintergrund und gibt sofort eine job_id zurück.
    Der eigentliche Lauf läuft in _run_job().
    """
    # Anzahl Leads defensiv normalisieren.
    try:
        max_leads = int(req.max_leads)
        if max_leads <= 0:
            max_leads = 5
    except (TypeError, ValueError):
        max_leads = 5

    job_id = uuid.uuid4().hex

    # Initialen Job-Status anlegen.
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "running",
            "current": 0,
            "total": 0,
            "current_name": "",
            "mode": req.mode,        # Modus wird gespeichert, aber NICHT für Versand genutzt.
            "leads": [],
            "error": None,
        }

    # Lauf als Hintergrund-Task einplanen.
    background_tasks.add_task(
        _run_job, job_id, req.keyword, req.city, max_leads, req.mode
    )

    return {"job_id": job_id}


@app.get("/status/{job_id}")
def status(job_id):
    """Gibt den aktuellen Fortschritt eines Jobs als JSON zurück."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        # Kopie zurückgeben, damit der Aufrufer nicht das Live-Dict hält.
        snapshot = dict(job) if job is not None else None

    if snapshot is None:
        return JSONResponse(
            status_code=404,
            content={"status": "error", "error": "Unbekannte job_id."},
        )

    return snapshot


@app.post("/send_single")
def send_single(req: SendRequest):
    """
    Versendet GENAU EINE Mail an einen einzelnen Prospect (Modus "direct").

    Sicherheit: Dieser Endpunkt wird ausschließlich durch einen bewussten
    Button-Klick pro Lead ausgelöst. Im Hintergrund-Lauf geht NICHTS automatisch
    raus – der Lauf erzeugt nur die Entwürfe.
    """
    result = send_single_mail(req.email, DIRECT_SUBJECT, req.message)

    # Nur echte, erfolgreiche Versände in den Verlauf schreiben. Test-Mails
    # (is_test=True) bleiben außen vor, um den Verlauf nicht zu verfälschen.
    if result == "ok" and not req.is_test:
        log_sent_mail(
            req.recipient_name,
            req.email,
            req.message,
            req.keyword,
            req.city,
        )

    return {"status": result}


@app.get("/history")
def history():
    """Gibt alle Verlaufseinträge als JSON-Liste zurück (neueste zuerst)."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, sent_at, recipient_name, recipient_email,
                       message, keyword, city
                FROM sent_mails
                ORDER BY sent_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]
    except Exception as exc:
        print("[DB] FEHLER bei /history:", exc)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": str(exc)},
        )


@app.get("/history/emails")
def history_emails():
    """
    Gibt nur die bereits kontaktierten E-Mail-Adressen zurück (eindeutig,
    lowercase) – Grundlage für die Doppel-Anschreiben-Warnung im Frontend.
    """
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT DISTINCT LOWER(recipient_email) FROM sent_mails "
                "WHERE recipient_email IS NOT NULL AND recipient_email != ''"
            ).fetchall()
        return [row[0] for row in rows]
    except Exception as exc:
        print("[DB] FEHLER bei /history/emails:", exc)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": str(exc)},
        )


# ---------------------------------------------------------------------------
# Test-Hilfsendpunkte (nur zum Prüfen der Doppel-Anschreiben-Warnung)
# ---------------------------------------------------------------------------

@app.get("/test/add_history")
def test_add_history():
    """Legt einen manuellen Test-Verlaufseintrag an."""
    try:
        log_sent_mail(
            "TEST Zahnpoint Mainz",
            "post@zahnpoint-mainz.de",
            "Dies ist ein manueller Testeintrag.",
            "Zahnarzt",
            "Mainz",
        )
        return {"status": "ok", "info": "Testeintrag angelegt"}
    except Exception as exc:
        print("[DB] FEHLER bei /test/add_history:", exc)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": str(exc)},
        )


@app.get("/test/clear_history")
def test_clear_history():
    """Löscht ALLE Verlaufseinträge (zum Aufräumen nach einem Test)."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM sent_mails")
        return {"status": "ok", "info": "Verlauf geleert"}
    except Exception as exc:
        print("[DB] FEHLER bei /test/clear_history:", exc)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": str(exc)},
        )


# ---------------------------------------------------------------------------
# HTML-Oberfläche (inline, mit eingebettetem CSS + JavaScript)
# ---------------------------------------------------------------------------

PAGE_HTML = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Avolane Outreach Agent</title>
<style>
  :root {
    --bg: #17082E;
    --bg-card: #1F0E3D;
    --accent: #7C3AED;
    --accent-light: #A78BFA;
    --text-light: #D8D4E8;
    --text-muted: #9A8FB8;
  }

  * { box-sizing: border-box; }

  body {
    margin: 0;
    padding: 0;
    background: var(--bg);
    color: var(--text-light);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    line-height: 1.5;
  }

  .wrap {
    max-width: 880px;
    margin: 0 auto;
    padding: 40px 20px 80px;
  }

  h1 {
    color: var(--accent-light);
    font-size: 2rem;
    margin: 0 0 4px;
    letter-spacing: 0.5px;
  }

  .subtitle {
    color: var(--text-muted);
    margin: 0 0 32px;
    font-size: 0.95rem;
  }

  .card {
    background: var(--bg-card);
    border: 1px solid rgba(167, 139, 250, 0.15);
    border-radius: 16px;
    padding: 24px;
    margin-bottom: 24px;
  }

  label {
    display: block;
    color: var(--accent-light);
    font-size: 0.85rem;
    margin-bottom: 6px;
    font-weight: 600;
  }

  input, select {
    width: 100%;
    padding: 11px 13px;
    margin-bottom: 18px;
    background: #2A1652;
    border: 1px solid rgba(167, 139, 250, 0.25);
    border-radius: 10px;
    color: var(--text-light);
    font-size: 0.95rem;
    outline: none;
  }

  input:focus, select:focus {
    border-color: var(--accent);
  }

  .row {
    display: flex;
    gap: 16px;
    flex-wrap: wrap;
  }
  .row > div { flex: 1; min-width: 160px; }

  button {
    background: var(--accent);
    color: #fff;
    border: none;
    border-radius: 12px;
    padding: 14px 26px;
    font-size: 1rem;
    font-weight: 700;
    cursor: pointer;
    box-shadow: 0 0 18px rgba(124, 58, 237, 0.55);
    transition: transform 0.05s ease, box-shadow 0.2s ease;
  }
  button:hover { box-shadow: 0 0 26px rgba(124, 58, 237, 0.8); }
  button:active { transform: translateY(1px); }
  button:disabled {
    opacity: 0.55;
    cursor: not-allowed;
    box-shadow: none;
  }

  /* Fortschritt */
  #progress { display: none; }
  .bar-track {
    width: 100%;
    height: 12px;
    background: #2A1652;
    border-radius: 8px;
    overflow: hidden;
    margin: 12px 0 10px;
  }
  .bar-fill {
    height: 100%;
    width: 0%;
    background: linear-gradient(90deg, var(--accent), var(--accent-light));
    transition: width 0.4s ease;
  }
  .progress-text { color: var(--text-muted); font-size: 0.9rem; }

  /* Ergebnis */
  #results { margin-top: 8px; }

  .lead-card {
    background: var(--bg-card);
    border: 1px solid rgba(167, 139, 250, 0.15);
    border-radius: 16px;
    padding: 20px 22px;
    margin-bottom: 18px;
  }
  .lead-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    margin-bottom: 8px;
  }
  .lead-name {
    color: var(--accent-light);
    font-size: 1.15rem;
    font-weight: 700;
    margin: 0;
  }
  .badge {
    background: var(--accent);
    color: #fff;
    border-radius: 999px;
    padding: 4px 12px;
    font-size: 0.8rem;
    font-weight: 700;
    white-space: nowrap;
  }
  .meta { color: var(--text-muted); font-size: 0.88rem; margin: 2px 0; }
  .meta a { color: var(--accent-light); text-decoration: none; }

  .tags { margin: 10px 0 4px; }
  .tag {
    display: inline-block;
    background: #2A1652;
    border: 1px solid rgba(167, 139, 250, 0.3);
    color: var(--text-light);
    border-radius: 8px;
    padding: 3px 9px;
    font-size: 0.78rem;
    margin: 0 6px 6px 0;
  }
  .tag.off { opacity: 0.4; }

  .message-box {
    margin-top: 12px;
    background: #130623;
    border: 1px solid rgba(167, 139, 250, 0.18);
    border-radius: 10px;
    padding: 14px 16px;
    color: var(--text-light);
    white-space: pre-wrap;
    font-size: 0.92rem;
  }

  .error-box {
    background: #3A1020;
    border: 1px solid #C0395B;
    color: #F2B8C6;
    border-radius: 12px;
    padding: 14px 16px;
    margin-top: 12px;
  }

  /* Direkt-senden (nur Modus "direct") */
  .direct-row {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-top: 12px;
    flex-wrap: wrap;
  }
  .send-btn {
    padding: 9px 16px;
    font-size: 0.9rem;
    border-radius: 10px;
    box-shadow: 0 0 12px rgba(124, 58, 237, 0.45);
  }
  .send-status { font-size: 0.88rem; font-weight: 600; }

  /* Tab-Navigation */
  .tabs { display: flex; gap: 8px; margin-bottom: 28px; }
  .tab-btn {
    background: var(--bg-card);
    color: var(--text-muted);
    border: 1px solid rgba(167, 139, 250, 0.2);
    border-radius: 10px;
    padding: 10px 22px;
    font-size: 0.95rem;
    font-weight: 700;
    box-shadow: none;
  }
  .tab-btn:hover { box-shadow: none; }
  .tab-btn.active {
    background: var(--accent);
    color: #fff;
    box-shadow: 0 0 18px rgba(124, 58, 237, 0.55);
  }

  /* Verlauf */
  .history-item {
    background: var(--bg-card);
    border: 1px solid rgba(167, 139, 250, 0.15);
    border-radius: 12px;
    padding: 14px 18px;
    margin-bottom: 12px;
  }
  .history-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    cursor: pointer;
    flex-wrap: wrap;
  }
  .history-name { color: var(--accent-light); font-weight: 700; }
  .history-email { color: var(--text-light); font-size: 0.9rem; }
  .history-date { color: var(--text-muted); font-size: 0.85rem; white-space: nowrap; }
  .history-msg {
    display: none;
    margin-top: 12px;
    background: #130623;
    border: 1px solid rgba(167, 139, 250, 0.18);
    border-radius: 10px;
    padding: 14px 16px;
    white-space: pre-wrap;
    font-size: 0.9rem;
  }
  .history-msg.open { display: block; }

  /* Doppel-Anschreiben-Warnung auf der Lead-Karte */
  .contacted-warn {
    display: inline-block;
    margin-top: 12px;
    background: #3A2410;
    border: 1px solid #E8862E;
    color: #F4C28A;
    border-radius: 8px;
    padding: 6px 12px;
    font-size: 0.85rem;
    font-weight: 700;
  }
</style>
</head>
<body>
<div class="wrap">
  <h1>Avolane Outreach Agent</h1>
  <p class="subtitle">Lokale Leads finden, bewerten und persönlich ansprechen.</p>

  <!-- Tab-Navigation -->
  <div class="tabs">
    <button class="tab-btn active" id="tabBtn-outreach" onclick="showTab('outreach')">Outreach</button>
    <button class="tab-btn" id="tabBtn-history" onclick="showTab('history')">Verlauf</button>
  </div>

  <!-- ==================== TAB: OUTREACH ==================== -->
  <div id="tab-outreach">

  <!-- Eingabemaske -->
  <div class="card">
    <div class="row">
      <div>
        <label for="keyword">Schlagwort</label>
        <input id="keyword" type="text" placeholder="z. B. Zahnarzt" value="">
      </div>
      <div>
        <label for="city">Stadt</label>
        <input id="city" type="text" placeholder="z. B. Mainz" value="">
      </div>
    </div>
    <div class="row">
      <div>
        <label for="max_leads">Anzahl Leads</label>
        <input id="max_leads" type="number" min="1" value="5">
      </div>
      <div>
        <label for="mode">Versand-Modus</label>
        <select id="mode">
          <option value="draft" selected>Nur Entwürfe (kein Versand)</option>
          <option value="report">Report an mich</option>
          <option value="direct">Direkt an Prospects</option>
        </select>
      </div>
    </div>
    <button id="startBtn" onclick="startRun()">Lauf starten</button>
  </div>

  <!-- Fortschritt -->
  <div class="card" id="progress">
    <strong style="color: var(--accent-light);">Lauf läuft …</strong>
    <div class="bar-track"><div class="bar-fill" id="barFill"></div></div>
    <div class="progress-text" id="progressText">Starte …</div>
  </div>

  <!-- Ergebnis -->
  <div id="results"></div>

  </div><!-- /tab-outreach -->

  <!-- ==================== TAB: VERLAUF ==================== -->
  <div id="tab-history" style="display: none;">
    <div class="card">
      <label for="historySearch">Verlauf durchsuchen</label>
      <input id="historySearch" type="text" oninput="filterHistory()"
             placeholder="Firmenname oder E-Mail …" value="" style="margin-bottom: 0;">
    </div>
    <div id="historyList"></div>
  </div><!-- /tab-history -->
</div>

<script>
  let pollTimer = null;
  let currentMode = "draft";  // Modus des zuletzt fertig gewordenen Laufs
  let currentKeyword = "";    // Suchbegriff des laufenden/letzten Laufs (für den Verlauf)
  let currentCity = "";       // Stadt des laufenden/letzten Laufs (für den Verlauf)
  let contactedEmails = new Set();  // bereits kontaktierte Adressen (lowercase) für die Doppel-Warnung

  // -------------------- Tab-Umschaltung --------------------
  function showTab(name) {
    const isHistory = name === "history";
    document.getElementById("tab-outreach").style.display = isHistory ? "none" : "block";
    document.getElementById("tab-history").style.display = isHistory ? "block" : "none";
    document.getElementById("tabBtn-outreach").classList.toggle("active", !isHistory);
    document.getElementById("tabBtn-history").classList.toggle("active", isHistory);
    if (isHistory) loadHistory();
  }

  // -------------------- Verlaufs-Ansicht --------------------
  let historyData = [];

  function loadHistory() {
    const container = document.getElementById("historyList");
    container.innerHTML = '<div class="card">Lade …</div>';
    fetch("/history")
      .then(r => r.json())
      .then(data => {
        historyData = Array.isArray(data) ? data : [];
        renderHistory();
      })
      .catch(err => {
        container.innerHTML = '<div class="error-box">Verlauf konnte nicht geladen werden: ' +
          escapeHtml(err.message) + "</div>";
      });
  }

  function filterHistory() { renderHistory(); }

  function renderHistory() {
    const container = document.getElementById("historyList");

    if (!historyData.length) {
      container.innerHTML = '<div class="card">Noch keine Mails versendet.</div>';
      return;
    }

    const q = (document.getElementById("historySearch").value || "").trim().toLowerCase();
    let list = historyData;
    if (q) {
      list = list.filter(e =>
        String(e.recipient_name || "").toLowerCase().includes(q) ||
        String(e.recipient_email || "").toLowerCase().includes(q));
    }

    if (!list.length) {
      container.innerHTML = '<div class="card">Keine Treffer für „' + escapeHtml(q) + '".</div>';
      return;
    }

    container.innerHTML = list.map((e, i) =>
      '<div class="history-item">' +
        '<div class="history-head" onclick="toggleHistoryMsg(' + i + ')">' +
          '<div>' +
            '<span class="history-name">' + escapeHtml(e.recipient_name || "—") + "</span> " +
            '<span class="history-email">' + escapeHtml(e.recipient_email || "") + "</span>" +
          "</div>" +
          '<span class="history-date">' + escapeHtml(formatDate(e.sent_at)) + "</span>" +
        "</div>" +
        '<div class="history-msg" id="historyMsg-' + i + '">' + escapeHtml(e.message || "") + "</div>" +
      "</div>"
    ).join("");
  }

  function toggleHistoryMsg(i) {
    const el = document.getElementById("historyMsg-" + i);
    if (el) el.classList.toggle("open");
  }

  // ISO-Zeitstempel -> "27.06.2026, 14:30"
  function formatDate(iso) {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso || "";
    const p = n => String(n).padStart(2, "0");
    return p(d.getDate()) + "." + p(d.getMonth() + 1) + "." + d.getFullYear() +
           ", " + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  function startRun() {
    const keyword = document.getElementById("keyword").value.trim();
    const city = document.getElementById("city").value.trim();
    const max_leads = parseInt(document.getElementById("max_leads").value, 10) || 5;
    const mode = document.getElementById("mode").value;

    if (!keyword || !city) {
      alert("Bitte Schlagwort und Stadt angeben.");
      return;
    }

    // Suchbegriff/Stadt des Laufs merken, damit sie beim Direktversand in den
    // Verlauf geschrieben werden können.
    currentKeyword = keyword;
    currentCity = city;

    // UI in den Lauf-Zustand versetzen.
    const btn = document.getElementById("startBtn");
    btn.disabled = true;
    document.getElementById("results").innerHTML = "";
    const progress = document.getElementById("progress");
    progress.style.display = "block";
    document.getElementById("barFill").style.width = "0%";
    document.getElementById("progressText").textContent = "Suche Leads …";

    fetch("/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keyword, city, max_leads, mode })
    })
      .then(r => r.json())
      .then(data => {
        if (!data.job_id) throw new Error("Keine job_id erhalten.");
        pollStatus(data.job_id);
      })
      .catch(err => {
        showError("Konnte Lauf nicht starten: " + err.message);
        btn.disabled = false;
        progress.style.display = "none";
      });
  }

  function pollStatus(jobId) {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(() => {
      fetch("/status/" + jobId)
        .then(r => r.json())
        .then(updateUI)
        .catch(err => {
          // Netzwerk-Hänger einfach beim nächsten Tick erneut versuchen.
          console.warn("Status-Abfrage fehlgeschlagen:", err);
        });
    }, 1500);
  }

  function updateUI(job) {
    const total = job.total || 0;
    const current = job.current || 0;
    const pct = total > 0 ? Math.round((current / total) * 100)
                          : (job.status === "running" ? 5 : 0);

    document.getElementById("barFill").style.width = pct + "%";

    if (job.status === "running") {
      let txt = "Suche Leads …";
      if (total > 0 && job.current_name) {
        txt = "Lead " + current + " von " + total + ": " + job.current_name;
      } else if (total > 0) {
        txt = "Lead " + current + " von " + total + " …";
      }
      document.getElementById("progressText").textContent = txt;
      return;
    }

    // Lauf beendet (done oder error) -> Polling stoppen, UI freigeben.
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    document.getElementById("startBtn").disabled = false;

    if (job.status === "error") {
      document.getElementById("progress").style.display = "none";
      showError("Lauf fehlgeschlagen: " + (job.error || "Unbekannter Fehler"));
      return;
    }

    // status === "done"
    document.getElementById("barFill").style.width = "100%";
    document.getElementById("progressText").textContent =
      "Fertig – " + (job.leads ? job.leads.length : 0) + " Leads verarbeitet.";
    currentMode = job.mode || "draft";   // bestimmt, ob Direkt-senden-Buttons erscheinen
    renderResults(job.leads || []);
    renderReportStatus(job.report_status);
  }

  // Report-Status-Banner (nur im Modus "report" gesetzt).
  function renderReportStatus(reportStatus) {
    if (!reportStatus) return;  // draft/direct oder noch nichts -> nichts anzeigen.

    const ok = reportStatus === "ok";
    const box = document.createElement("div");
    box.style.borderRadius = "12px";
    box.style.padding = "12px 16px";
    box.style.marginBottom = "18px";
    box.style.fontSize = "0.92rem";
    box.style.fontWeight = "600";

    if (ok) {
      box.style.background = "#0F2E1A";
      box.style.border = "1px solid #2FA86A";
      box.style.color = "#9BE8BE";
      box.textContent = "Report an hallo@avolane.de gesendet ✓";
    } else {
      box.style.background = "#3A1020";
      box.style.border = "1px solid #C0395B";
      box.style.color = "#F2B8C6";
      box.textContent = "Report-Versand fehlgeschlagen: " + reportStatus;
    }

    const results = document.getElementById("results");
    results.insertBefore(box, results.firstChild);
  }

  function showError(msg) {
    document.getElementById("results").innerHTML =
      '<div class="error-box">' + escapeHtml(msg) + "</div>";
  }

  // Lesbare Beschriftungen für die Feature-Flags.
  const FEATURE_LABELS = {
    hat_buchungstool: "Buchungstool",
    hat_chatbot: "Chatbot",
    hat_kontaktformular: "Kontaktformular",
    hat_social_media: "Social Media"
  };

  // Hält die aktuell angezeigten Leads, damit die Direkt-senden-Buttons per
  // Index auf den jeweiligen Lead (E-Mail + Nachricht) zugreifen können.
  let currentLeads = [];

  function renderResults(leads) {
    const container = document.getElementById("results");
    if (!leads.length) {
      container.innerHTML = '<div class="card">Keine Leads gefunden.</div>';
      return;
    }

    // Sicherheitshalber clientseitig nach Score absteigend sortieren.
    leads.sort((a, b) => (b.score || 0) - (a.score || 0));

    currentLeads = leads;

    // Im Modus "direct" zuerst die bereits kontaktierten Adressen laden, damit
    // die Doppel-Anschreiben-Warnung direkt beim ersten Rendern sichtbar ist.
    if (currentMode === "direct") {
      fetch("/history/emails")
        .then(r => r.json())
        .then(list => {
          contactedEmails = new Set((Array.isArray(list) ? list : []).map(e => String(e).toLowerCase()));
        })
        .catch(() => { contactedEmails = new Set(); })
        .finally(paintResults);
    } else {
      contactedEmails = new Set();
      paintResults();
    }
  }

  // Zeichnet die aktuell gehaltenen Leads (currentLeads) in den Ergebnisbereich.
  function paintResults() {
    const container = document.getElementById("results");

    // Im Modus "direct": optionales Test-Empfänger-Feld ÜBER der Lead-Liste.
    let testBlock = "";
    if (currentMode === "direct") {
      testBlock = buildTestRecipientBlock();
    }

    container.innerHTML = testBlock + currentLeads.map(buildCard).join("");

    // Button-Beschriftungen an den (ggf. leeren) Test-Empfänger anpassen.
    if (currentMode === "direct") updateSendButtons();
  }

  // Eingabefeld für einen Test-Empfänger (nur Modus "direct"). Steht hier eine
  // Adresse, gehen ALLE Sende-Buttons gefahrlos an diese statt an die Prospects.
  function buildTestRecipientBlock() {
    return '' +
      '<div class="card" id="testRecipientBox">' +
        '<label for="testRecipient">Test-Empfänger (optional)</label>' +
        '<input id="testRecipient" type="text" oninput="updateSendButtons()" ' +
          'placeholder="leer lassen = echter Versand an Prospects" value="">' +
        '<div class="meta" style="margin-top: 8px;">Wenn hier eine Adresse steht, ' +
          'gehen ALLE Sende-Buttons an diese Adresse statt an die Prospects – ' +
          'zum gefahrlosen Testen.</div>' +
      "</div>";
  }

  // Liest den aktuellen Test-Empfänger aus dem Eingabefeld (leer = nicht gesetzt).
  function getTestRecipient() {
    const field = document.getElementById("testRecipient");
    return field ? field.value.trim() : "";
  }

  // Prüft, ob die Adresse eines Leads bereits im Verlauf steht.
  function isContacted(lead) {
    return !!(lead && lead.email && contactedEmails.has(lead.email.toLowerCase()));
  }

  // Passt Text/Farbe aller Sende-Buttons an Test-Empfänger und Verlauf an.
  function updateSendButtons() {
    const testEmail = getTestRecipient();
    const orangeShadow = "0 0 12px rgba(232, 134, 46, 0.55)";
    currentLeads.forEach((lead, idx) => {
      const btn = document.getElementById("sendBtn-" + idx);
      if (!btn || btn.dataset.sent === "1") return;  // bereits gesendete in Ruhe lassen
      if (testEmail) {
        // Test-Versand: alle Buttons gehen an die Test-Adresse.
        btn.textContent = "TEST → an " + testEmail + " senden";
        btn.style.background = "#E8862E";
        btn.style.boxShadow = orangeShadow;
      } else if (isContacted(lead)) {
        // Echter Versand, aber Adresse wurde bereits kontaktiert -> orange warnen.
        btn.textContent = "An " + lead.email + " senden";
        btn.style.background = "#E8862E";
        btn.style.boxShadow = orangeShadow;
      } else {
        btn.textContent = "An " + lead.email + " senden";
        btn.style.background = "";
        btn.style.boxShadow = "";
      }
    });
  }

  function buildCard(lead, idx) {
    const features = lead.features || {};
    const tags = Object.keys(FEATURE_LABELS).map(key => {
      const on = !!features[key];
      const mark = on ? "✓ " : "— ";  // Haken vs. Gedankenstrich
      return '<span class="tag ' + (on ? "" : "off") + '">' +
        mark + escapeHtml(FEATURE_LABELS[key]) + "</span>";
    }).join("");

    let meta = "";
    if (lead.address) meta += '<div class="meta">' + escapeHtml(lead.address) + "</div>";
    if (lead.phone)   meta += '<div class="meta">Tel.: ' + escapeHtml(lead.phone) + "</div>";
    if (lead.website) meta += '<div class="meta">Web: <a href="' + escapeAttr(lead.website) +
                              '" target="_blank" rel="noopener">' + escapeHtml(lead.website) + "</a></div>";
    if (lead.email)   meta += '<div class="meta">E-Mail: <a href="mailto:' + escapeAttr(lead.email) +
                              '">' + escapeHtml(lead.email) + "</a></div>";

    // Direkt-senden-Block NUR im Modus "direct" und nur wenn eine E-Mail
    // vorhanden ist. Der Versand passiert ausschließlich per Klick.
    let directBlock = "";
    if (currentMode === "direct" && lead.email) {
      // Doppel-Anschreiben-Warnung, falls die Adresse schon im Verlauf steht.
      const warn = isContacted(lead)
        ? '<div class="contacted-warn">⚠ Bereits kontaktiert</div>'
        : "";
      directBlock =
        warn +
        '<div class="direct-row">' +
          '<button class="send-btn" id="sendBtn-' + idx + '" ' +
            'onclick="sendSingle(' + idx + ')">An ' + escapeHtml(lead.email) + ' senden</button>' +
          '<span class="send-status" id="sendStatus-' + idx + '"></span>' +
        "</div>";
    }

    return '' +
      '<div class="lead-card">' +
        '<div class="lead-head">' +
          '<p class="lead-name">' + escapeHtml(lead.name || "Unbenannt") + "</p>" +
          '<span class="badge">Score ' + (lead.score != null ? lead.score : 0) + "/100</span>" +
        "</div>" +
        meta +
        '<div class="tags">' + tags + "</div>" +
        '<div class="message-box">' + escapeHtml(lead.message || "") + "</div>" +
        directBlock +
      "</div>";
  }

  // Versendet GENAU EINE Mail an den Prospect des angeklickten Leads.
  function sendSingle(idx) {
    const lead = currentLeads[idx];
    if (!lead || !lead.email) return;

    const btn = document.getElementById("sendBtn-" + idx);
    const statusEl = document.getElementById("sendStatus-" + idx);

    // Zieladresse: Test-Empfänger falls gesetzt, sonst die echte Prospect-Adresse.
    const testEmail = getTestRecipient();
    const target = testEmail || lead.email;

    // Doppel-Anschreiben-Warnung nur beim echten Versand (nicht bei Test).
    if (!testEmail && isContacted(lead)) {
      if (!confirm("Diese Adresse wurde bereits kontaktiert. Trotzdem senden?")) {
        return;
      }
    }

    // Sicherheitsabfrage vor dem echten Versand.
    if (!confirm("Diese Nachricht jetzt wirklich an " + target + " senden?")) {
      return;
    }

    btn.disabled = true;
    statusEl.style.color = "#9A8FB8";
    statusEl.textContent = "Sende …";

    fetch("/send_single", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: target,
        message: lead.message || "",
        is_test: !!testEmail,   // Test-Empfänger gesetzt -> nicht in den Verlauf
        recipient_name: lead.name || "",
        keyword: currentKeyword,
        city: currentCity
      })
    })
      .then(r => r.json())
      .then(data => {
        if (data.status === "ok") {
          btn.dataset.sent = "1";
          // Bei echtem Versand die Adresse als kontaktiert merken, damit ein
          // erneuter Klick gewarnt wird (Test-Versand zählt nicht).
          if (!testEmail && lead.email) contactedEmails.add(lead.email.toLowerCase());
          statusEl.style.color = "#9BE8BE";
          statusEl.textContent = "✓ Gesendet";
          btn.textContent = "Gesendet";
        } else {
          statusEl.style.color = "#F2B8C6";
          statusEl.textContent = "Fehler: " + (data.status || "unbekannt");
          btn.disabled = false;
        }
      })
      .catch(err => {
        statusEl.style.color = "#F2B8C6";
        statusEl.textContent = "Fehler: " + err.message;
        btn.disabled = false;
      });
  }

  // Einfache HTML-Escapes gegen kaputtes Markup / Injection.
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function escapeAttr(s) {
    return escapeHtml(s);
  }
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Startblock
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
