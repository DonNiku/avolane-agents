"""
outreach_agent.py

B2B-Outreach-Agent für eine deutsche AI-Automatisierungsagentur.

Ablauf:
  1. Lokale Unternehmen über SerpAPI (Google Maps) finden.
  2. Jede Website scrapen und auf vorhandene Automatisierungs-Features prüfen.
  3. Leads nach Automatisierungspotenzial scoren.
  4. Pro Lead eine personalisierte, ehrlich interessierte Erstnachricht
     mit Claude generieren.
  5. Ergebnis nach Score sortiert ausgeben und als JSON speichern.

Benötigte Umgebungsvariablen (.env):
  ANTHROPIC_API_KEY  – API-Key für die Anthropic/Claude-API
  SERPAPI_KEY        – API-Key für SerpAPI (google-search-results)
"""

import os
import re
import json
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Drittanbieter-SDKs
from serpapi import GoogleSearch          # pip install google-search-results
import anthropic                          # pip install anthropic


# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

# Umgebungsvariablen aus .env laden
load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SERPAPI_KEY = os.getenv("SERPAPI_KEY")

# Claude-Modell für die Nachrichtengenerierung
CLAUDE_MODEL = "claude-sonnet-4-6"

# HTTP-Header, damit Websites uns nicht sofort als Bot abweisen
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    )
}

# Pause (Sekunden) zwischen den Scraping-Aufrufen, um Websites nicht zu überlasten
SCRAPE_DELAY = 1.5

# Stadtteil-Expansion: SerpAPI liefert pro Google-Maps-Suche max. ~20 Ergebnisse.
# Indem wir pro Stadtteil suchen, umgehen wir dieses Limit und finden mehr Leads.
CITY_DISTRICTS = {
    "Mainz": [
        "Mainz-Mombach",
        "Gonsenheim",
        "Hechtsheim",
        "Weisenau",
        "Bretzenheim",
        "Finthen",
        "Altstadt",
    ],
}


# ---------------------------------------------------------------------------
# 2. Lead-Suche
# ---------------------------------------------------------------------------

def search_leads(keyword, city, max_leads):
    """
    Sucht lokale Unternehmen über SerpAPI (engine="google_maps").

    Wenn die Stadt in CITY_DISTRICTS steht, wird über alle Stadtteile iteriert,
    um das ~20-Ergebnis-Limit pro Suche zu umgehen.

    Gibt eine Liste von Dicts zurück: {name, website, phone, address}.
    Duplikate werden anhand des (normalisierten) Namens vermieden.
    """
    # Suchgebiete bestimmen: entweder die Stadtteile oder die Stadt selbst.
    search_areas = CITY_DISTRICTS.get(city, [city])

    leads = []
    seen_names = set()  # zur Duplikat-Erkennung (normalisierte Namen)
    seen_phones = set()  # zur Duplikat-Erkennung (normalisierte Telefonnummern)
    seen_websites = set()  # zur Duplikat-Erkennung (normalisierte Web-Domains)

    for area in search_areas:
        # Sobald wir genug Leads haben, brechen wir ab.
        if len(leads) >= max_leads:
            break

        query = f"{keyword} in {area}"
        print(f"[Suche] {query} ...")

        params = {
            "engine": "google_maps",
            "q": query,
            "type": "search",
            "hl": "de",
            "api_key": SERPAPI_KEY,
        }

        # Netzwerk-/API-Aufruf robust kapseln – ein Fehler bei einem Stadtteil
        # darf nicht den ganzen Lauf abbrechen.
        try:
            search = GoogleSearch(params)
            results = search.get_dict()
        except Exception as exc:
            print(f"  [Fehler] SerpAPI-Aufruf für '{area}' fehlgeschlagen: {exc}")
            continue

        local_results = results.get("local_results", []) or []

        for place in local_results:
            if len(leads) >= max_leads:
                break

            name = (place.get("title") or "").strip()
            if not name:
                continue

            # Duplikate anhand von Name, Telefonnummer und Web-Domain vermeiden.
            norm_name = name.lower()

            # Telefonnummer auf reine Ziffern normalisieren.
            norm_phone = re.sub(r"\D", "", place.get("phone") or "")

            # Web-Domain normalisieren: Schema und führendes "www." entfernen,
            # alles ab "?" abschneiden, lowercase, nur Host bis zum ersten "/".
            norm_web = (place.get("website") or "").strip().lower()
            norm_web = norm_web.replace("http://", "").replace("https://", "")
            if norm_web.startswith("www."):
                norm_web = norm_web[4:]
            norm_web = norm_web.split("?")[0]
            norm_web = norm_web.split("/")[0]

            # Lead überspringen, wenn Name, Telefon oder Domain bereits gesehen.
            if (
                norm_name in seen_names
                or (norm_phone and norm_phone in seen_phones)
                or (norm_web and norm_web in seen_websites)
            ):
                continue

            seen_names.add(norm_name)
            if norm_phone:
                seen_phones.add(norm_phone)
            if norm_web:
                seen_websites.add(norm_web)

            leads.append({
                "name": name,
                "website": (place.get("website") or "").strip(),
                "phone": (place.get("phone") or "").strip(),
                "address": (place.get("address") or "").strip(),
            })

        # Kleine Pause zwischen den Stadtteil-Suchen.
        time.sleep(1.0)

    print(f"[Suche] {len(leads)} eindeutige Leads gefunden.")
    return leads[:max_leads]


# ---------------------------------------------------------------------------
# 3. Website-Scraping
# ---------------------------------------------------------------------------

# Bestandteile, die eine gefundene Adresse als unecht entlarven
# (Tracking-Mails, Platzhalter, in Dateinamen/Bildern eingebettete "@" usw.).
EMAIL_BLOCKLIST = ["sentry", "example", ".png", ".jpg", "@2x", "wixpress"]


def extract_email(soup, text):
    """
    Extrahiert aus einer bereits geparsten BeautifulSoup-Instanz (soup) und
    dem sichtbaren Text eine Kontakt-E-Mail-Adresse.

    Vorgehen:
      1. Zuerst alle mailto:-Links durchsuchen und die erste gültige Adresse
         nehmen.
      2. Falls keine gefunden: im Text per Regex nach E-Mail-Adressen suchen.
      3. Adressen herausfiltern, die offensichtlich keine echten
         Kontaktadressen sind (siehe EMAIL_BLOCKLIST).
      4. Die erste plausible Adresse als String zurückgeben, sonst leeren
         String. Robuste Fehlerbehandlung – kein Crash.
    """
    email_pattern = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"

    def is_plausible(candidate):
        lowered = candidate.lower()
        return not any(bad in lowered for bad in EMAIL_BLOCKLIST)

    try:
        # 1. mailto:-Links durchsuchen.
        if soup is not None:
            for link in soup.find_all("a", href=True):
                href = link["href"].strip()
                if href.lower().startswith("mailto:"):
                    # Adresse aus dem href lösen (Schema und ggf. ?-Parameter).
                    address = href[len("mailto:"):].split("?")[0].strip()
                    match = re.search(email_pattern, address)
                    if match and is_plausible(match.group(0)):
                        return match.group(0)

        # 2. Fallback: im sichtbaren Text suchen.
        for match in re.findall(email_pattern, text or ""):
            if is_plausible(match):
                return match
    except Exception as exc:
        print(f"  [Fehler] E-Mail-Extraktion fehlgeschlagen: {exc}")

    return ""


# Schlüsselwörter, die auf eine Kontakt-/Impressumsseite hindeuten.
SUBPAGE_KEYWORDS = ["impressum", "kontakt", "contact", "legal"]


def find_email_on_subpages(base_url, soup):
    """
    Fallback-Suche: Wenn auf der Startseite keine E-Mail gefunden wurde,
    werden Impressum-/Kontaktseiten aufgerufen und dort gesucht.

    1. Durchsucht alle a-Tags nach Links, deren sichtbarer Text ODER href
       auf eine Kontakt-/Impressumsseite hindeutet (SUBPAGE_KEYWORDS).
    2. Baut absolute URLs (urljoin mit base_url), sammelt max. 3 Kandidaten
       ohne Duplikate.
    3. Ruft jede Kandidaten-URL nacheinander auf, parst sie und wendet
       extract_email an.
    4. Gibt die erste gefundene E-Mail zurück, sonst leeren String.
       Fehler pro URL werden abgefangen – kein Abbruch.
    """
    candidates = []

    try:
        if soup is not None:
            for link in soup.find_all("a", href=True):
                href = link["href"].strip()
                link_text = link.get_text(separator=" ").strip().lower()
                haystack = href.lower() + " " + link_text
                if any(kw in haystack for kw in SUBPAGE_KEYWORDS):
                    absolute = urljoin(base_url, href)
                    if absolute not in candidates:
                        candidates.append(absolute)
                if len(candidates) >= 3:
                    break
    except Exception as exc:
        print(f"  [Fehler] Konnte Unterseiten-Links nicht ermitteln: {exc}")
        return ""

    for index, candidate in enumerate(candidates):
        # Kleine Pause zwischen den Aufrufen.
        if index > 0:
            time.sleep(1)

        try:
            response = requests.get(candidate, headers=REQUEST_HEADERS, timeout=10)
            response.raise_for_status()

            sub_soup = BeautifulSoup(response.text, "html.parser")
            sub_text = re.sub(r"\s+", " ", sub_soup.get_text(separator=" ")).strip()

            email = extract_email(sub_soup, sub_text)
            if email:
                return email
        except Exception as exc:
            print(f"  [Fehler] Konnte Unterseite '{candidate}' nicht laden: {exc}")
            continue

    return ""


def scrape_website(url):
    """
    Holt die Seite mit requests (Timeout 10s, User-Agent-Header),
    parst mit BeautifulSoup und gibt ein Dict zurück:
      {"text": <sichtbarer Text max. 3000 Zeichen>, "email": <Adresse oder "">}

    Die E-Mail wird auf der soup ermittelt, BEVOR die nicht sichtbaren Tags
    per decompose entfernt werden (sonst wären die mailto:-Links weg).

    Bei jedem Fehler wird {"text": "", "email": ""} zurückgegeben – kein Crash.
    """
    if not url:
        return {"text": "", "email": ""}

    try:
        response = requests.get(url, headers=REQUEST_HEADERS, timeout=10)
        response.raise_for_status()
    except Exception as exc:
        print(f"  [Fehler] Konnte '{url}' nicht laden: {exc}")
        return {"text": "", "email": ""}

    try:
        soup = BeautifulSoup(response.text, "html.parser")

        # Sichtbaren Text für die E-Mail-Suche im Fließtext vorbereiten.
        raw_text = re.sub(r"\s+", " ", soup.get_text(separator=" ")).strip()

        # E-Mail ermitteln, SOLANGE die mailto-Links noch vorhanden sind.
        email = extract_email(soup, raw_text)

        # Fallback: Wenn die Startseite keine E-Mail lieferte, Impressum-/
        # Kontaktseiten durchsuchen (soup VOR dem decompose nutzen).
        if not email:
            email = find_email_on_subpages(url, soup)

        # Nicht sichtbare / irrelevante Elemente entfernen.
        for tag in soup(["script", "style", "noscript", "head", "meta", "link"]):
            tag.decompose()

        text = soup.get_text(separator=" ")
        # Whitespace zusammenfassen.
        text = re.sub(r"\s+", " ", text).strip()
        return {"text": text[:3000], "email": email}
    except Exception as exc:
        print(f"  [Fehler] Konnte '{url}' nicht parsen: {exc}")
        return {"text": "", "email": ""}


# ---------------------------------------------------------------------------
# 4. Feature-Erkennung
# ---------------------------------------------------------------------------

def detect_website_features(text):
    """
    Analysiert den gescrapten Text (zusammen mit ggf. vorhandenen Links)
    und gibt ein Dict mit Booleans zurück:

      hat_buchungstool    – Doctolib, Calendly, Samedi, ...
      hat_chatbot         – Intercom, Tidio, LiveChat, ...
      hat_kontaktformular – Formular-/Kontakt-Hinweise
      hat_social_media    – Instagram, Facebook, LinkedIn
    """
    lower = (text or "").lower()

    def contains_any(keywords):
        return any(kw in lower for kw in keywords)

    return {
        "hat_buchungstool": contains_any([
            "doctolib", "calendly", "samedi", "terminbuchung",
            "termin buchen", "online termin", "booking",
        ]),
        "hat_chatbot": contains_any([
            "intercom", "tidio", "livechat", "live chat",
            "chatbot", "crisp.chat", "drift",
        ]),
        "hat_kontaktformular": contains_any([
            "kontaktformular", "contact form", "nachricht senden",
            "schreiben sie uns", "kontaktieren sie uns", "formular",
        ]),
        "hat_social_media": contains_any([
            "instagram", "facebook", "linkedin", "tiktok",
            "twitter", "x.com",
        ]),
    }


# ---------------------------------------------------------------------------
# 5. Lead-Scoring
# ---------------------------------------------------------------------------

def score_lead(lead, features):
    """
    Vergibt einen Score von 0–100 basierend auf dem Automatisierungspotenzial.

    Grundgedanke: Je mehr manueller Aufwand vermutet wird, desto höher das
    Potenzial – und damit der Score.

      - Fehlendes Buchungstool  -> hoher manueller Termin-Aufwand
      - Fehlender Chatbot       -> Anfragen werden manuell beantwortet
      - Vorhandene Telefonnummer-> Kommunikation läuft (auch) telefonisch
      - Nur Kontaktformular      -> immer noch manuelle Bearbeitung
      - Keine Website             -> sehr wenig Digitalisierung, viel Potenzial
    """
    score = 0

    # Fehlendes Buchungstool = viel manueller Termin-Aufwand.
    if not features.get("hat_buchungstool"):
        score += 35

    # Fehlender Chatbot = Anfragen werden manuell beantwortet.
    if not features.get("hat_chatbot"):
        score += 30

    # Telefonnummer vorhanden = Kommunikation läuft (auch) telefonisch/manuell.
    if lead.get("phone"):
        score += 20

    # Kontaktformular ohne Automatisierung = weiterhin manuelle Bearbeitung.
    if features.get("hat_kontaktformular"):
        score += 10

    # Keine Website = sehr geringe Digitalisierung = hohes Potenzial.
    if not lead.get("website"):
        score += 15

    # Aktive Social-Media-Präsenz spricht für etwas digitale Reife
    # -> minimal geringeres Potenzial.
    if features.get("hat_social_media"):
        score -= 10

    # Auf den Bereich 0–100 begrenzen.
    return max(0, min(100, score))


# ---------------------------------------------------------------------------
# 6. Nachrichtengenerierung
# ---------------------------------------------------------------------------

# Wörter, die im fertigen Nachrichtentext absolut nicht vorkommen dürfen.
# Es geht um PROZESSE und LÖSUNGEN, nicht um Technologie.
FORBIDDEN_WORDS = [
    "KI",
    "AI",
    "künstliche Intelligenz",
    "Automatisierung",
    "Tool",
    "Software",
    "Agentur",
    "Technologie",
    "digitalisieren",
]


def _find_forbidden_words(text):
    """
    Gibt eine Liste der im Text gefundenen verbotenen Wörter zurück
    (Groß-/Kleinschreibung wird ignoriert). Leere Liste = sauber.
    """
    lower = (text or "").lower()
    found = []
    for word in FORBIDDEN_WORDS:
        # Wortgrenzen, damit z. B. "Aikido" nicht wegen "AI" anschlägt.
        pattern = r"\b" + re.escape(word.lower()) + r"\b"
        if re.search(pattern, lower):
            found.append(word)
    return found


def _extract_text(response):
    """Fügt die Textblöcke einer Anthropic-Antwort zu einem String zusammen."""
    parts = [block.text for block in response.content if hasattr(block, "text")]
    return "".join(parts).strip()


def generate_message(lead, features, variant_index=0):
    """
    Erzeugt mit Claude eine personalisierte deutsche Erstnachricht
    (LinkedIn / E-Mail).

    Prinzip:
      - Die Nachricht setzt an einem konkret beobachteten MANUELLEN PROZESS an
        (aus den erkannten Features abgeleitet), nicht nur an einer vagen
        Beobachtung. Beispiel: "keine Online-Terminbuchung erkennbar" =>
        Termine laufen vermutlich manuell per Telefon/Mail.
      - KEIN Direktverkauf, KEINE Terminanfrage, KEINE Produkterklärung.
      - Nur EINE einzige echte, ehrlich interessierte Frage, die sich auf den
        konkret benannten Prozess bezieht. Eine mögliche Lösung darf höchstens
        implizit anklingen, wird aber NICHT angeboten oder erklärt.
      - Ton: locker, auf Augenhöhe, als Gründer aus Mainz, der selbst aus der
        Praxis kommt. Maximal 4–5 Sätze.
      - Bestimmte Technologie-Wörter sind absolut verboten (siehe
        FORBIDDEN_WORDS); es geht um Abläufe, Zeit, Aufwand und einfachere Wege.

    Eine kleine Sicherheitsprüfung kontrolliert die Antwort auf verbotene Wörter
    und lässt Claude die Nachricht bei Bedarf EINMAL ohne diese Wörter neu
    schreiben.

    Gibt nur den reinen Nachrichtentext zurück.
    """
    if not ANTHROPIC_API_KEY:
        return "[Fehler] ANTHROPIC_API_KEY fehlt – keine Nachricht generiert."

    # Sechs grundverschiedene Einstiegs-Stile (als Anweisung an den Schreiber,
    # nicht als fertige Sätze). Über variant_index wird durchrotiert, damit die
    # Nachrichten strukturell nicht wie aus einer Vorlage wirken.
    OPENING_STYLES = [
        "Starte direkt mit der konkreten Beobachtung zum Ablauf, ohne Vorrede.",
        "Starte mit einer kurzen, ehrlichen Bemerkung aus eigener Erfahrung, "
        "dann die Beobachtung.",
        "Starte direkt mit der einen Frage, die Beobachtung kommt danach knapp.",
        "Starte mit einem kurzen, konkreten Bezug zur Branche des Unternehmens.",
        "Starte mit einer beiläufigen Beobachtung (\"Mir ist aufgefallen ...\"), "
        "sehr locker.",
        "Starte mit einer kurzen Anerkennung von etwas, das die Firma gut macht, "
        "dann die Beobachtung.",
    ]
    chosen_style = OPENING_STYLES[variant_index % len(OPENING_STYLES)]

    # Der "ich bin aus Mainz"-Bezug darf NICHT in jeder Nachricht vorkommen:
    # nur bei geradem variant_index erlaubt, sonst komplett weglassen.
    if variant_index % 2 == 0:
        mainz_regel = (
            "MAINZ-BEZUG: Du darfst (musst aber nicht) beiläufig erwähnen, dass "
            "du selbst aus Mainz kommst – höchstens einmal und ganz nebenbei."
        )
    else:
        mainz_regel = (
            "MAINZ-BEZUG: Erwähne in dieser Nachricht NICHT, dass du aus Mainz "
            "kommst, und beziehe dich nicht auf Mainz."
        )

    # Aus den erkannten Features den wahrscheinlichen manuellen Prozess ableiten.
    # So bekommt Claude konkrete Aufhänger statt nur "Feature fehlt".
    prozess_hinweise = []

    if not features.get("hat_buchungstool"):
        prozess_hinweise.append(
            "Keine Online-Terminbuchung erkennbar – Termine werden vermutlich "
            "manuell per Telefon oder Mail vereinbart und hin- und hergeschoben."
        )
    else:
        prozess_hinweise.append(
            "Online-Terminbuchung vorhanden – Termine laufen also schon recht "
            "selbstständig."
        )

    if not features.get("hat_chatbot"):
        prozess_hinweise.append(
            "Kein Chat erkennbar – wiederkehrende Fragen von Kunden werden "
            "vermutlich jedes Mal von Hand beantwortet."
        )
    else:
        prozess_hinweise.append(
            "Chat vorhanden – einfache Fragen werden teils schon direkt "
            "aufgefangen."
        )

    if features.get("hat_kontaktformular"):
        prozess_hinweise.append(
            "Kontaktformular vorhanden – Anfragen landen vermutlich als Mail und "
            "müssen einzeln nachverfolgt und beantwortet werden."
        )

    if features.get("hat_social_media"):
        prozess_hinweise.append(
            "Aktiv auf Social Media – jemand pflegt das also nebenher mit."
        )

    prozess_block = "\n- ".join(prozess_hinweise)

    # Verbotene Wörter für den Prompt aufbereiten.
    verbotene_woerter_str = ", ".join(f'"{w}"' for w in FORBIDDEN_WORDS)

    base_prompt = f"""Du bist ein sympathischer Gründer aus Mainz, der selbst aus der Praxis kommt
und weiß, wie viel Zeit kleine Abläufe im Arbeitsalltag fressen können.
Du schreibst einem lokalen Unternehmen eine allererste Nachricht (LinkedIn oder E-Mail).

Unternehmen: {lead.get('name')}
Adresse: {lead.get('address') or 'unbekannt'}
Website: {lead.get('website') or 'keine'}
Telefon: {lead.get('phone') or 'unbekannt'}

Wahrscheinliche manuelle Abläufe (aus der Website abgeleitet):
- {prozess_block}

Schreibe eine kurze, persönliche deutsche Erstnachricht nach diesen Regeln:
- Setze konkret an EINEM beobachteten manuellen ABLAUF an (z. B. dass Termine
  vermutlich von Hand per Telefon/Mail vereinbart werden). Benenne diesen
  Ablauf greifbar – nicht nur "mir ist etwas aufgefallen".
- EINSTIEG (verbindliche Vorgabe): Eröffne die Nachricht so: {chosen_style}
  Beginne NICHT mit "ich bin selbst aus Mainz und schaue mir an, wie lokale
  Unternehmen ihren Alltag organisieren" oder Ähnlichem. Wirke nicht wie eine
  Vorlage. Vermeide die Floskel "wie lokale Praxen/Unternehmen ihren Alltag
  organisieren" komplett.
- {mainz_regel}
- Stelle nur EINE einzige echte, ehrlich interessierte Frage zu genau diesem
  Ablauf. WEICHERE FRAGE: Sie soll im allerersten Kontakt offen und leicht zu
  beantworten sein, nicht ausfragend. KEINE mehrteiligen Fragen (also nicht
  "Wie viel Zeit ... und hängt das an einer Person?"). Eine einzige, einfache,
  offene Frage, die der Empfänger locker in einem Satz beantworten kann und die
  NICHT nach Zahlen oder einem Aufwand-Audit klingt. Sie soll Neugier zeigen,
  nicht messen.
- KEIN Direktverkauf, KEINE Terminanfrage, KEINE Produkterklärung. Eine mögliche
  Lösung darf höchstens ganz leise implizit anklingen, wird aber NICHT angeboten
  und NICHT erklärt.
- Formuliere alles in Begriffen von ABLÄUFEN, ZEIT, AUFWAND und einfacheren
  WEGEN – nicht in Begriffen von Technik.
- ABSOLUTES WORTVERBOT: Die folgenden Wörter dürfen NICHT vorkommen (auch nicht
  in Abwandlungen): {verbotene_woerter_str}.
- ANREDE: Verwende durchgängig die höfliche Anrede "Sie" (Siezen). Niemals
  duzen. Das gilt für die gesamte Nachricht inklusive Begrüßung und Abschluss.
- Ton: locker, auf Augenhöhe, als Gründer aus Mainz, der die Praxis kennt.
  Nie aufdringlich.
- Maximal 4–5 Sätze.
- Gib NUR den reinen Nachrichtentext aus, ohne Betreff, ohne Anführungszeichen,
  ohne Erklärungen drumherum."""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        # Erster Versuch.
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": base_prompt}],
        )
        message_text = _extract_text(response)

        # Sicherheitsprüfung: verbotene Wörter aufspüren.
        offenders = _find_forbidden_words(message_text)
        if offenders:
            # Claude EINMAL automatisch auffordern, ohne diese Wörter neu zu schreiben.
            offenders_str = ", ".join(f'"{w}"' for w in offenders)
            retry_messages = [
                {"role": "user", "content": base_prompt},
                {"role": "assistant", "content": message_text},
                {"role": "user", "content": (
                    f"Deine Nachricht enthält verbotene Wörter: {offenders_str}. "
                    "Schreibe die Nachricht komplett neu, ohne diese Wörter (und "
                    "ohne sinngleiche Technik-Begriffe). Bleib bei demselben "
                    "konkreten Ablauf und derselben einen Frage, gleicher Ton, "
                    "maximal 4–5 Sätze. Gib NUR den reinen Nachrichtentext aus."
                )},
            ]
            retry_response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=400,
                messages=retry_messages,
            )
            message_text = _extract_text(retry_response)

        return message_text
    except Exception as exc:
        print(f"  [Fehler] Nachrichtengenerierung fehlgeschlagen: {exc}")
        return "[Fehler] Nachricht konnte nicht generiert werden."


# ---------------------------------------------------------------------------
# 7. Orchestrierung
# ---------------------------------------------------------------------------

def run_agent(keyword, city, max_leads):
    """
    Orchestriert den gesamten Ablauf:
      1. Leads suchen
      2. Pro Lead: Website scrapen, Features erkennen, scoren, Nachricht generieren
      3. Nach Score absteigend sortieren
      4. Ergebnis als outreach_results.json speichern

    Gibt die (sortierte) Lead-Liste zurück.
    """
    leads = search_leads(keyword, city, max_leads)
    enriched_leads = []

    for index, lead in enumerate(leads, start=1):
        print(f"\n[{index}/{len(leads)}] Verarbeite: {lead['name']}")

        # Website scrapen (robust – gibt notfalls leeres Dict zurück).
        result = scrape_website(lead.get("website", ""))
        text = result["text"]
        email = result["email"]

        # Features erkennen (bekommt weiterhin nur den Text).
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

        # Kurze Pause zwischen den Scraping-/API-Durchläufen, um Websites
        # und APIs nicht zu überlasten.
        if index < len(leads):
            time.sleep(SCRAPE_DELAY)

    # Nach Score absteigend sortieren.
    enriched_leads.sort(key=lambda lead: lead["score"], reverse=True)

    # Ergebnis als JSON speichern (robust gekapselt).
    try:
        with open("outreach_results.json", "w", encoding="utf-8") as f:
            json.dump(enriched_leads, f, ensure_ascii=False, indent=2)
        print("\n[Gespeichert] Ergebnis in outreach_results.json")
    except Exception as exc:
        print(f"\n[Fehler] Konnte Ergebnis nicht speichern: {exc}")

    return enriched_leads


# ---------------------------------------------------------------------------
# 8. Interaktiver Einstiegspunkt
# ---------------------------------------------------------------------------

def main():
    """Interaktiver Lauf über die Konsole."""
    print("=== B2B-Outreach-Agent ===\n")

    keyword = input("Schlagwort (z. B. Zahnarzt, Friseur, Steuerberater): ").strip()
    city = input("Stadt (z. B. Mainz): ").strip()

    # Anzahl Leads robust einlesen.
    raw_max = input("Anzahl Leads (z. B. 10): ").strip()
    try:
        max_leads = int(raw_max)
        if max_leads <= 0:
            raise ValueError
    except ValueError:
        print("Ungültige Zahl – verwende Standardwert 10.")
        max_leads = 10

    # Agent ausführen.
    leads = run_agent(keyword, city, max_leads)

    # Lesbare Zusammenfassung pro Lead ausgeben.
    print("\n\n=== Ergebnis (nach Score sortiert) ===")
    if not leads:
        print("Keine Leads gefunden.")
        return

    for index, lead in enumerate(leads, start=1):
        print("\n" + "-" * 60)
        print(f"{index}. {lead['name']}  (Score: {lead['score']}/100)")
        if lead.get("website"):
            print(f"   Website:  {lead['website']}")
        if lead.get("phone"):
            print(f"   Telefon:  {lead['phone']}")
        if lead.get("address"):
            print(f"   Adresse:  {lead['address']}")
        print(f"\n   Nachricht:\n   {lead['message']}")


if __name__ == "__main__":
    main()
