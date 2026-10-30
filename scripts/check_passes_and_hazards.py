#!/usr/bin/env python3
"""
Prueft alle 3 Stunden:
1. alpen-paesse.ch (IG Alpenpässe) auf Sperrungen der Zentralschweiz-relevanten
   Alpenpaesse - dort wird jeder Pass einzeln mit eigenem Zeitstempel
   gepflegt, was deutlich aktueller ist als das zuvor verwendete TCS-Portal
2. Alertswiss PolyAlert-Feed (inoffizieller Feed, siehe HINWEIS unten) auf
   aktive Warnungen/Gefahrengebiete in der Zentralschweiz

Schreibt das Ergebnis nach data/auto-sites.json. Diese Datei wird bei jedem
Lauf komplett neu geschrieben - manuelle Eintraege gehoeren in
data/manual-sites.json und werden von diesem Skript nie beruehrt.

HINWEIS zu Alertswiss: Der Bund (BABS) bietet noch keine offiziell
dokumentierte oeffentliche API fuer Polyalert-Daten an (eine standardisierte
Schnittstelle "CAP Suisse" ist erst fuer 2027 angekuendigt). Der hier
verwendete Feed ist derselbe, den auch inoffizielle Community-Projekte
(z.B. Home-Assistant-Integrationen) nutzen. Er kann sich jederzeit ohne
Vorankuendigung aendern oder wegfallen.
"""
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ALPENPAESSE_URL = "https://www.alpen-paesse.ch/de/"
ALERTSWISS_URL = (
    "https://www.alert.swiss/content/alertswiss-internet/de/home/"
    "_jcr_content/polyalert.alertswiss_alerts.actual.json"
)

# Zentralschweiz-Bounding-Box (etwas grosszuegiger als der Kartenausschnitt,
# damit Grimsel-/Furkapass mit abgedeckt sind)
BBOX = {"min_lat": 46.50, "max_lat": 47.20, "min_lon": 7.95, "max_lon": 8.95}

# Fest hinterlegte Zentralschweiz-relevante Paesse: Slug (= URL-Pfad auf
# alpen-paesse.ch), Anzeigename und Koordinaten (Passhoehe, via swisstopo
# geocodiert).
PASSES = [
    {"slug": "sustenpass", "name": "Sustenpass", "lat": 46.7313, "lon": 8.4512},
    {"slug": "klausenpass", "name": "Klausenpass", "lat": 46.8682, "lon": 8.8555},
    {"slug": "grimselpass", "name": "Grimselpass", "lat": 46.5610, "lon": 8.3370},
    {"slug": "furkapass", "name": "Furkapass", "lat": 46.5719, "lon": 8.4164},
    {"slug": "pragelpass", "name": "Pragelpass", "lat": 46.9992, "lon": 8.8695},
    {"slug": "glaubenbergpass", "name": "Glaubenbergpass", "lat": 46.8930, "lon": 8.1074},
    {
        "slug": "gotthardpass", "name": "Gotthardpass", "lat": 46.5593, "lon": 8.5612,
        # gotthard-traffic.ch selbst laesst sich nicht automatisiert abfragen (Cloudflare-
        # Turnstile-Captcha blockiert jeden Skriptzugriff) - daher nur ein Link zum
        # manuellen Nachschauen der aktuellen Stausituation vor dem Tunnel.
        "extra_detail": {
            "label": "Stau-Info",
            "value": '<a href="https://www.gotthard-traffic.ch/?lan=de" target="_blank" rel="noopener">Aktuelle Stausituation ansehen (gotthard-traffic.ch)</a>',
        },
    },
]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; strassensperrungen-karte-bot/1.0)"}


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def check_passes():
    """Gibt (erfolgreich, sites) zurueck. Bei erfolgreich=False soll der
    Aufrufer die zuletzt bekannten Pass-Eintraege unveraendert beibehalten,
    statt sie durch eine leere Liste zu ersetzen.

    Quelle: alpen-paesse.ch (IG Alpenpässe) statt TCS. Dort wird jeder Pass
    einzeln mit eigenem Zeitstempel gepflegt ("Unveraendert gueltig seit"),
    was in der Praxis deutlich aktueller ist als das TCS-Portal, das alle
    Paesse nur mit einem gemeinsamen, selten aktualisierten Stand zeigt.
    """
    try:
        html = fetch(ALPENPAESSE_URL)
    except Exception as e:
        print(f"WARNUNG: alpen-paesse.ch nicht erreichbar: {e}", file=sys.stderr)
        return False, []
    sites = []
    for p in PASSES:
        idx = html.find(f'pass-card-title-inner"><a href="/de/alpenpaesse/{p["slug"]}"')
        if idx == -1:
            print(f"WARNUNG: {p['name']} nicht auf alpen-paesse.ch gefunden (Slug geaendert?)", file=sys.stderr)
            continue
        chunk = html[idx:idx + 1200]

        status_icon_m = re.search(r'icon-status-(\w+)', chunk)
        status_icon = status_icon_m.group(1) if status_icon_m else ""
        is_closed = status_icon in ("closed", "winter")

        badge_m = re.search(
            r'pass-card-badge-text"><div class="pass-card-badge-text-clamp-1">([^<]+)</div>'
            r'<div class="pass-card-badge-text-clamp-1">([^<]+)</div>',
            chunk,
        )
        status_text = badge_m.group(1).strip() if badge_m else ("Geschlossen" if is_closed else "Offen")
        seit = re.sub(r"^Unverändert gültig seit:+\s*", "", badge_m.group(2).strip()) if badge_m else "unbekannt"

        pass_url = f"{ALPENPAESSE_URL}alpenpaesse/{p['slug']}"
        details = [
            {"label": "Status", "value": status_text},
            {"label": "Unverändert seit", "value": seit},
            {"label": "Quelle", "value": f'<a href="{pass_url}" target="_blank" rel="noopener">alpen-paesse.ch</a>'},
        ]
        if "extra_detail" in p:
            details.append(p["extra_detail"])

        # Jeder Pass bekommt IMMER einen Eintrag - geschlossen (blau) oder offen
        # (gruen mit Haken) - damit auf der Karte auch sichtbar ist, welche
        # Paesse aktuell befahrbar sind, nicht nur die gesperrten.
        sites.append({
            "id": f"pass-{p['slug']}",
            "name": p["name"],
            "typ": "passsperrung" if is_closed else "pass_offen",
            "lat": p["lat"],
            "lon": p["lon"],
            "pdf": None,
            "details": details,
            "link": pass_url,
        })
    if not sites:
        # Seite erreichbar, aber kein einziger Pass erkannt -> Markup hat sich
        # vermutlich geaendert. Nicht als Erfolg werten, sonst wuerden die
        # zuletzt bekannten Pass-Eintraege durch eine leere Liste ersetzt.
        print("WARNUNG: kein einziger Pass erkannt - alpen-paesse.ch-Markup geaendert?", file=sys.stderr)
        return False, []
    return True, sites


def polygon_centroid(coordinates):
    lats = [float(c[0]) for c in coordinates]
    lons = [float(c[1]) for c in coordinates]
    return sum(lats) / len(lats), sum(lons) / len(lons)


def in_bbox(lat, lon):
    return (BBOX["min_lat"] <= lat <= BBOX["max_lat"]
            and BBOX["min_lon"] <= lon <= BBOX["max_lon"])


def check_alertswiss():
    """Gibt (erfolgreich, sites) zurueck, siehe check_passes()."""
    try:
        raw = fetch(ALERTSWISS_URL)
        data = json.loads(raw)
    except Exception as e:
        print(f"WARNUNG: Alertswiss-Feed nicht erreichbar: {e}", file=sys.stderr)
        return False, []

    sites = []
    for alert in data.get("alerts", []):
        if alert.get("testAlert") or alert.get("technicalTestAlert") or alert.get("allClear"):
            continue

        match_lat, match_lon = None, None
        for area in alert.get("areas", []):
            for poly in area.get("polygons", []):
                coords = poly.get("coordinates", [])
                if not coords:
                    continue
                lat, lon = polygon_centroid(coords)
                if in_bbox(lat, lon):
                    match_lat, match_lon = lat, lon
                    break
            if match_lat is not None:
                break
        if match_lat is None:
            continue

        sites.append({
            "id": f"alertswiss-{alert.get('identifier', '')}",
            "name": alert.get("title", {}).get("title", "Alertswiss-Warnung"),
            "typ": "gefahrengebiet",
            "lat": match_lat,
            "lon": match_lon,
            "pdf": None,
            "details": [
                {"label": "Ereignis", "value": alert.get("event", "unbekannt")},
                {"label": "Schweregrad", "value": alert.get("severity", "unbekannt")},
                {"label": "Herausgeber", "value": alert.get("publisherName", "unbekannt")},
                {"label": "Gemeldet", "value": alert.get("sent", "unbekannt")},
                {"label": "Quelle", "value": "Alertswiss (inoffizieller Feed, siehe Hinweis)"},
            ],
            "link": alert.get("link"),
        })
    return True, sites


def main():
    out_path = Path(__file__).resolve().parent.parent / "data" / "auto-sites.json"

    try:
        previous = json.loads(out_path.read_text(encoding="utf-8"))
    except Exception:
        previous = []
    previous_passes = [s for s in previous if s.get("typ") in ("passsperrung", "pass_offen")]
    previous_hazards = [s for s in previous if s.get("typ") == "gefahrengebiet"]

    passes_ok, passes = check_passes()
    hazards_ok, hazards = check_alertswiss()

    # Bei einem Fehlschlag die zuletzt bekannten Eintraege dieser Quelle
    # beibehalten, statt sie durch eine leere Liste zu ersetzen.
    final_passes = passes if passes_ok else previous_passes
    final_hazards = hazards if hazards_ok else previous_hazards

    sites = final_passes + final_hazards
    out_path.write_text(json.dumps(sites, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # Zeitstempel des letzten Checks separat ablegen, damit die Karte anzeigen kann,
    # wann die Pass-/Alertswiss-Daten zuletzt geprueft wurden (unabhaengig vom Inhalt
    # von auto-sites.json, das bei einem Fehlschlag ja auch mal unveraendert bleibt).
    meta_path = out_path.parent / "last-check.json"
    meta = {
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "passes_ok": passes_ok,
        "hazards_ok": hazards_ok,
    }
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(
        f"{len(sites)} automatisch erkannte Einträge geschrieben nach {out_path} "
        f"(Pässe: {'aktuell' if passes_ok else 'ALT/unveraendert, Quelle nicht erreichbar'}, "
        f"Alertswiss: {'aktuell' if hazards_ok else 'ALT/unveraendert, Quelle nicht erreichbar'})"
    )


if __name__ == "__main__":
    main()
