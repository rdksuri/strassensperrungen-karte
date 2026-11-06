#!/usr/bin/env python3
"""
Stau-Meldungen auf der Strecke Altdorf - Goeschenen (A2 und Kantonsstrasse H2)
fuer die Dienstbetrieb-Seite des Rettungsdienstes Uri.

Quelle: opentransportdata.swiss "Road traffic - traffic situations"
(DATEX II, Verkehrsmeldungen von ASTRA/VMZ-CH und Polizei), alle 15 Min.
Token als GitHub-Secret OTD_KEY.

Format der Meldungstexte (Beispiele aus echten Daten):
  "Freigegeben: A2 Luzern -> Gotthard zwischen Anschluss Erstfeld und Anschluss Amsteg
   Sachlage: Stau, Länge [km] 6 ..."
  "Freigegeben: H2 Göschenen <-> Flüelen Anschluss Göschenen Sachlage: Verkehrsbehinderung Baustelle ..."
"Luzern -> Gotthard" = Richtung Sueden, "Gotthard -> Luzern" = Richtung Norden,
"<->" = beide Richtungen.

Schreibt data/gotthard-stau.json:
  { "checked", "ok", "fehler",
    "sued":  {"stau": bool, "ab", "bis", "km", "minuten", "text"},   # A2 Richtung Gotthard
    "nord":  {...},                                                  # A2 Richtung Altdorf
    "h2":    {...},                                                  # Kantonsstrasse, beliebige Richtung
    "tunnel": Text falls Gotthard-Strassentunnel gesperrt,
    "weitere": [Sperrungen/Unfaelle im Abschnitt] }

Aufruf mit --debug schreibt die relevanten Rohmeldungen ins Log.
"""
import gzip
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path

URL = "https://api.opentransportdata.swiss/TDP/Soap_Datex2/TrafficSituations/Pull"
SOAP_ACTION = "http://opentransportdata.swiss/TDP/Soap_Datex2/Pull/v1/pullTrafficMessages"
BODY = """<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" xmlns:dx223="http://datex2.eu/schema/2/2_0">
<SOAP-ENV:Body><dx223:d2LogicalModel modelBaseVersion="2"><dx223:exchange><dx223:supplierIdentification>
<dx223:country>ch</dx223:country><dx223:nationalIdentifier>RD Uri</dx223:nationalIdentifier>
</dx223:supplierIdentification></dx223:exchange></dx223:d2LogicalModel></SOAP-ENV:Body></SOAP-ENV:Envelope>"""

OUT = Path(__file__).resolve().parent.parent / "data" / "gotthard-stau.json"
DEBUG = "--debug" in sys.argv

# Orte der Strecke Altdorf -> Goeschenen (inkl. Gotthard-Strassentunnel Nordseite)
UR_ORT = re.compile(r"\b(Seedorf|Fl(?:ü|ue)elen|Altdorf|Schattdorf|B(?:ü|ue)rglen|Erstfeld|Silenen|Amsteg|"
                    r"Gurtnellen|Intschi|Wassen|Pfaffensprung|Naxberg|G(?:ö|oe)schenen|Gotthard-Tunnel|"
                    r"Gotthard-Strassentunnel)\b", re.I)
# Zielorte im Kopf der Meldung, die "Richtung Sueden" bedeuten
SUED_ZIEL = re.compile(r"^(Gotthard|Chiasso|Airolo|Bellinzona|G(?:ö|oe)schenen|Andermatt)", re.I)
NORD_ZIEL = re.compile(r"^(Luzern|Basel|Fl(?:ü|ue)elen|Altdorf|Brunnen|Z(?:ü|ue)rich)", re.I)
LOC_START = (r"(?:zwischen|Anschluss|Ortschaft|Tunnel|Parkplatz|Kreuzung|Autobahndreieck|Verzweigung|"
             r"Rastplatz|Rastst(?:ä|ae)tte|Grenz(?:ü|ue)bergang|Dosierstelle|Br(?:ü|ue)cke|Galerie|Pass)\b")
KOPF = re.compile(r"^(?:[\wäöü]+:\s*)?(?P<strasse>[AHN]\d+)\s+(?P<von>.+?)\s*(?P<pfeil><->|->|<-)\s*(?P<nach>.+?)\s+"
                  r"(?P<ort>" + LOC_START + r".*?)\s+Sachlage:\s*(?P<lage>.*)$", re.S)
STAU_TYP = {"stationaryTraffic", "queuingTraffic", "slowTraffic", "heavyTraffic", "stopAndGo"}
STAU_TXT = re.compile(r"\bStau\b|stockend|Kolonne|Wartezeit|zähflüssig|zaehfluessig", re.I)
WICHTIG_TXT = re.compile(r"gesperrt|Unfall|Panne|Gegenstand|Wintersperre|Lawine|Murgang|Steinschlag|Glätte|Schnee", re.I)


def local(tag):
    return tag.rsplit("}", 1)[-1]


def xsi_type(el):
    for k, v in el.attrib.items():
        if local(k) == "type":
            return v.split(":")[-1]
    return ""


def kommentar(rec):
    out = []
    for e in rec.iter():
        if local(e.tag) == "generalPublicComment":
            for v in e.iter():
                if local(v.tag) == "value" and (v.text or "").strip():
                    lang = next((val for k, val in v.attrib.items() if local(k) == "lang"), "")
                    out.append((lang, re.sub(r"\s+", " ", v.text.strip())))
    de = [t for l, t in out if l.lower().startswith("de")] or [t for _, t in out]
    return " ".join(dict.fromkeys(de))


def first(el, name):
    for e in el.iter():
        if local(e.tag) == name and (e.text or "").strip():
            return e.text.strip()
    return None


def zeit(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def holen(key):
    req = urllib.request.Request(URL, data=BODY.encode(), method="POST", headers={
        "Authorization": "Bearer " + key,
        "Content-Type": "text/xml; charset=utf-8",
        "Accept-Encoding": "gzip",
        "SOAPAction": SOAP_ACTION,
        "User-Agent": "RD-Uri-Stauanzeige/1.0 (rdksuri.github.io)",
    })
    with urllib.request.urlopen(req, timeout=90) as r:
        data = r.read()
        enc = r.headers.get("Content-Encoding", "")
    if data[:2] == b"\x1f\x8b" or "gzip" in enc:
        data = gzip.decompress(data)
    return data


def ortsname(s):
    s = re.sub(r"^(?:Autobahndreieck\s+)?(Anschluss|Ortschaft|Tunnel|Parkplatz|Kreuzung|Verzweigung|Rastplatz|Raststätte|Dosierstelle)\s+",
               "", s.strip(), flags=re.I)
    return s.replace("Fluelen", "Flüelen").replace("Goeschenen", "Göschenen").strip(" ,.")


def ab_bis(ort):
    z = re.match(r"zwischen\s+(.+?)\s+und\s+(.+)$", ort, re.I)
    if z:
        return ortsname(z.group(1)), ortsname(z.group(2))
    return ortsname(ort), None


def km_min(lage, rec):
    km = None
    k = re.search(r"L(?:ä|ae)nge\s*\[km\]\s*([\d\.,]+)", lage) or re.search(r"(\d+(?:[\.,]\d+)?)\s*km\b", lage)
    if k:
        try:
            km = float(k.group(1).replace(",", "."))
        except ValueError:
            pass
    if km is None and first(rec, "lengthAffected"):
        try:
            km = round(float(first(rec, "lengthAffected")) / 1000, 1)
        except ValueError:
            pass
    minuten = None
    s = re.search(r"(\d+)\s*(?:Std|Stunde)n?\.?(?:\s*(\d+)\s*Min)?", lage)
    m = re.search(r"(\d+)\s*Min", lage)
    if s:
        minuten = int(s.group(1)) * 60 + int(s.group(2) or 0)
    elif m:
        minuten = int(m.group(1))
    else:
        d = first(rec, "delayTimeValue")
        if d:
            try:
                minuten = round(float(d) / 60)
            except ValueError:
                pass
    return km, minuten


def auswerten(xml_bytes):
    root = ET.fromstring(xml_bytes)
    jetzt = datetime.now(timezone.utc)
    res = {"sued": {"stau": False}, "nord": {"stau": False}, "h2": {"stau": False},
           "tunnel": None, "weitere": []}
    n = 0
    for rec in root.iter():
        if local(rec.tag) != "situationRecord":
            continue
        text = kommentar(rec)
        if not text:
            continue
        kopf = KOPF.match(text)
        if not kopf:
            continue
        strasse, nach, pfeil = kopf["strasse"].upper(), kopf["nach"].strip(), kopf["pfeil"]
        ort, lage = kopf["ort"].strip(), kopf["lage"].strip()
        if strasse not in ("A2", "N2", "H2") or not UR_ORT.search(ort):
            continue
        # nur aktuell gueltige Meldungen
        start, ende = zeit(first(rec, "overallStartTime")), zeit(first(rec, "overallEndTime"))
        if (start and start > jetzt) or (ende and ende < jetzt):
            continue
        n += 1
        typ, abn = xsi_type(rec), first(rec, "abnormalTrafficType") or ""
        ist_stau = (abn in STAU_TYP) or bool(STAU_TXT.search(lage.split("Dauer:")[0]))
        if DEBUG:
            print("-----", typ, abn, "|", text[:500])
        ab, bis = ab_bis(ort)
        km, minuten = km_min(lage, rec)
        eintrag = {"stau": True, "ab": ab, "bis": bis, "km": km, "minuten": minuten, "text": text}

        if strasse in ("A2", "N2") and re.search(r"Gotthard-(Strassen)?tunnel", ort, re.I) \
                and re.search(r"(Tunnel|Strecke)\s+gesperrt", lage, re.I):
            res["tunnel"] = text
            continue
        if ist_stau:
            if strasse == "H2":
                if not res["h2"]["stau"]:
                    res["h2"] = eintrag
                continue
            sued = pfeil == "<->" or (pfeil == "->" and SUED_ZIEL.search(nach)) or \
                (pfeil == "<-" and SUED_ZIEL.search(kopf["von"]))
            nord = pfeil == "<->" or (pfeil == "->" and NORD_ZIEL.search(nach)) or \
                (pfeil == "<-" and NORD_ZIEL.search(kopf["von"]))
            if sued and not res["sued"]["stau"]:
                res["sued"] = eintrag
            if nord and not res["nord"]["stau"]:
                res["nord"] = eintrag
        elif WICHTIG_TXT.search(lage.split("Dauer:")[0]) and "Baustelle" not in lage.split("Dauer:")[0]:
            res["weitere"].append(text)
    res["weitere"] = list(dict.fromkeys(res["weitere"]))[:6]
    print(f"{n} aktuelle Meldungen im Abschnitt Altdorf-Göschenen")
    return res


def main():
    key = os.environ.get("OTD_KEY", "").strip()
    jetzt = datetime.now(timezone.utc).isoformat(timespec="seconds")
    res = {"checked": jetzt, "ok": False, "fehler": None,
           "sued": {"stau": False}, "nord": {"stau": False}, "h2": {"stau": False},
           "tunnel": None, "weitere": []}
    try:
        if not key:
            raise RuntimeError("Kein Token (OTD_KEY) hinterlegt")
        res.update(auswerten(holen(key)))
        res["ok"] = True
        print("Süd:", res["sued"], "\nNord:", res["nord"], "\nH2:", res["h2"], "\nTunnel:", res["tunnel"],
              "\nWeitere:", res["weitere"])
    except urllib.error.HTTPError as e:
        res["fehler"] = f"HTTP {e.code}"
        print("HTTP-Fehler", e.code, e.read()[:300])
    except Exception as e:  # noqa: BLE001
        res["fehler"] = str(e)[:200]
        print("Fehler:", e)

    # Nur schreiben, wenn sich der Inhalt geaendert hat oder der letzte Stand
    # aelter als ca. 1 h ist (sonst 96 Commits pro Tag).
    alt = {}
    if OUT.exists():
        try:
            alt = json.loads(OUT.read_text(encoding="utf-8"))
        except ValueError:
            alt = {}
    ohne_zeit = lambda d: {k: v for k, v in d.items() if k != "checked"}
    zu_alt = True
    if alt.get("checked"):
        zu_alt = datetime.now(timezone.utc) - datetime.fromisoformat(alt["checked"]) > timedelta(minutes=55)
    if ohne_zeit(alt) != ohne_zeit(res) or zu_alt:
        OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        print("geschrieben")
    else:
        print("unveraendert")


if __name__ == "__main__":
    main()
