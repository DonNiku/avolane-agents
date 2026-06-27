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

import uuid
import threading

import uvicorn
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

# Einzelschritte aus dem bestehenden Agenten importieren – so können wir den
# Fortschritt Lead für Lead anzeigen, statt nur run_agent() blind aufzurufen.
from outreach_agent import (
    search_leads,
    scrape_website,
    detect_website_features,
    score_lead,
    generate_message,
)


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
    mode: str = "draft"  # "draft" | "report" | "direct" (Etappe A: nur durchgereicht)


def _update_job(job_id, **fields):
    """Aktualisiert ein Job-Status-Dict threadsicher."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is not None:
            job.update(fields)


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

        # 4. Job als fertig markieren.
        _update_job(
            job_id,
            status="done",
            current=total,
            current_name="",
            leads=enriched_leads,
        )

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
</style>
</head>
<body>
<div class="wrap">
  <h1>Avolane Outreach Agent</h1>
  <p class="subtitle">Lokale Leads finden, bewerten und persönlich ansprechen.</p>

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
</div>

<script>
  let pollTimer = null;

  function startRun() {
    const keyword = document.getElementById("keyword").value.trim();
    const city = document.getElementById("city").value.trim();
    const max_leads = parseInt(document.getElementById("max_leads").value, 10) || 5;
    const mode = document.getElementById("mode").value;

    if (!keyword || !city) {
      alert("Bitte Schlagwort und Stadt angeben.");
      return;
    }

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
    renderResults(job.leads || []);
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

  function renderResults(leads) {
    const container = document.getElementById("results");
    if (!leads.length) {
      container.innerHTML = '<div class="card">Keine Leads gefunden.</div>';
      return;
    }

    // Sicherheitshalber clientseitig nach Score absteigend sortieren.
    leads.sort((a, b) => (b.score || 0) - (a.score || 0));

    container.innerHTML = leads.map(buildCard).join("");
  }

  function buildCard(lead) {
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

    return '' +
      '<div class="lead-card">' +
        '<div class="lead-head">' +
          '<p class="lead-name">' + escapeHtml(lead.name || "Unbenannt") + "</p>" +
          '<span class="badge">Score ' + (lead.score != null ? lead.score : 0) + "/100</span>" +
        "</div>" +
        meta +
        '<div class="tags">' + tags + "</div>" +
        '<div class="message-box">' + escapeHtml(lead.message || "") + "</div>" +
      "</div>";
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
