#!/usr/bin/env python3
"""
Stau-Meldungen A2 Altdorf - Goeschenen (Gotthard-Achse) fuer die
Dienstbetrieb-Seite des Rettungsdienstes Uri.

Quelle: opentransportdata.swiss "Road traffic - traffic situations"
(DATEX II, Verkehrsmeldungen von ASTRA/VMZ-CH und Polizei).
Token als GitHub-Secret OTD_KEY.

Schreibt data/gotthard-stau.json:
  {
    "checked": ISO-Zeit, "ok": true/false, "fehler": "...",
    "sued": {"stau": bool, "ab": "Amsteg", "bis": "Göschenen", "km": 8,
             "minuten": 40, "text": "Originalmeldung"},
    "nord": {...gleich...},
    "tunnel": "Text falls Gotthard-Strassentunnel gesperrt" | null,
    "weitere": ["andere Meldungen A2 Uri (Unfall, Baustelle ...)"]
  }

Aufruf mit --debug schreibt die Rohstruktur der Uri-Meldungen ins Log.
"""
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

# Orte entlang der Strecke, Nord -> Sued. Nur Meldungen, die einen dieser
# Orte nennen, sind fuer die Fahrt Altdorf -> Andermatt relevant.
ORTE = ["Seelisberg", "Flüelen", "Altdorf", "Schattdorf", "Erstfeld", "Amsteg",
        "Gurtnellen", "Intschi", "Wassen", "Göschenen", "Gotthard", "Airolo"]
ORT_RE = re.compile(r"(Seelisberg|Fl(?:ü|ue)elen|Altdorf|Schattdorf|Erstfeld|Amsteg|Gurtnellen|"
                    r"Intschi|Wassen|G(?:ö|oe)schenen|Gotthard|Airolo)", re.I)
SUED_RE = re.compile(r"Richtung\s+(S(?:ü|ue)den|Chiasso|Gotthard|Tessin|Airolo|Lugano)|"
                     r"→\s*(Chiasso|Gotthard|Airolo)|Basel\s*(?:→|->|-)\s*Chiasso", re.I)
NORD_RE = re.compile(r"Richtung\s+(Norden|Basel|Luzern|Z(?:ü|ue)rich|Altdorf)|"
                     r"→\s*(Basel|Luzern)|Chiasso\s*(?:→|->|-)\s*Basel", re.I)
STAU_RE = re.compile(r"Stau|stockend|Kolonne|Verkehrs(?:ü|ue)berlastung|Wartezeit", re.I)


def local(tag):
    return tag.rsplit("}", 1)[-1]


def xsi_type(el):
    for k, v in el.attrib.items():
        if local(k) == "type":
            return v.split(":")[-1]
    return ""


def texts(el, name):
    """Alle Texte unterhalb von Elementen mit Namen `name` (namespace-unabhaengig)."""
    out = []
    for e in el.iter():
        if local(e.tag) == name:
            for v in e.iter():
                if local(v.tag) == "value" and (v.text or "").strip():
                    lang = next((val for k, val in v.attrib.items() if local(k) == "lang"), "")
                    out.append((lang, v.text.strip()))
    return out


def first(el, name):
    for e in el.iter():
        if local(e.tag) == name and (e.text or "").strip():
            return e.text.strip()
    return None


def norm(o):
    o = o.replace("Fluelen", "Flüelen").replace("Goeschenen", "Göschenen")
    return o[:1].upper() + o[1:]


def holen(key):
    req = urllib.request.Request(URL, data=BODY.encode(), method="POST", headers={
        "Authorization": "Bearer " + key,
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": SOAP_ACTION,
        "User-Agent": "RD-Uri-Stauanzeige/1.0 (rdksuri.github.io)",
    })
    with urllib.request.urlopen(req, timeout=90) as r:
        data = r.read()
        ctype = r.headers.get("Content-Type", "")
        enc = r.headers.get("Content-Encoding", "")
    if data[:2] == b"\x1f\x8b" or "gzip" in enc:
        import gzip
        data = gzip.decompress(data)
    if "--debug" in sys.argv:
        print("Content-Type:", ctype, "| Encoding:", enc, "| Bytes:", len(data))
        print("Anfang:", data[:400])
    # SOAP-Antwort kann als multipart/MTOM kommen: XML-Teil herausschneiden
    i = data.find(b"<")
    j = data.rfind(b">")
    if i > 0 or "multipart" in ctype:
        data = data[i:j + 1]
        k = data.find(b"--uuid")
        if k > 0:
            data = data[:k].rstrip()
            data = data[:data.rfind(b">") + 1]
    return data


def auswerten(xml_bytes, debug=False):
    root = ET.fromstring(xml_bytes)
    meldungen = []
    for sit in root.iter():
        if local(sit.tag) != "situation":
            continue
        for rec in sit:
            if local(rec.tag) != "situationRecord":
                continue
            kommentare = texts(rec, "generalPublicComment") or texts(rec, "comment")
            de = [t for l, t in kommentare if l.lower().startswith("de")] or [t for _, t in kommentare]
            text = " ".join(dict.fromkeys(de))
            loc_names = " ".join(t for _, t in texts(rec, "alertCLocationName"))
            road = first(rec, "roadNumber") or ""
            alles = " ".join([text, loc_names, road])
            if not ORT_RE.search(alles):
                continue
            if road and road.upper() not in ("A2", "N2", "2") and "A2" not in alles and "Gotthard" not in alles:
                # nur Autobahn A2 / Gotthard; Kantonsstrasse-Meldungen separat
                pass
            m = {
                "typ": xsi_type(rec),
                "text": text,
                "orte": loc_names,
                "strasse": road,
                "laenge_m": first(rec, "lengthAffected") or first(rec, "queueLength"),
                "verzoegerung_s": first(rec, "delayTimeValue"),
                "richtung": first(rec, "alertCDirectionCoded") or first(rec, "directionBound") or "",
                "abnormal": first(rec, "abnormalTrafficType") or "",
                "start": first(rec, "overallStartTime"),
            }
            meldungen.append(m)
            if debug:
                print("-----", m["typ"], "|", m["strasse"], "|", m["richtung"])
                print("TEXT:", m["text"][:600])
                print("ORTE:", m["orte"][:300], "| Laenge:", m["laenge_m"], "| Verz.:", m["verzoegerung_s"],
                      "| abnormal:", m["abnormal"])
    return meldungen


def stau_aus(m):
    t = m["text"] + " " + m["orte"]
    ab = bis = None
    z = re.search(r"zwischen\s+([\wÄÖÜäöü\-\. ]+?)\s+und\s+([\wÄÖÜäöü\-\. ]+?)(?:[,\.;]|\s+(?:in|Richtung|Stau|stockend|Kolonne)|$)", t)
    if z:
        ab, bis = z.group(1).strip(), z.group(2).strip()
    else:
        orte = [norm(o) for o in ORT_RE.findall(t)]
        if orte:
            ab = orte[0]
            bis = orte[-1] if len(orte) > 1 else None
    km = None
    k = re.search(r"(\d+(?:[\.,]\d+)?)\s*km", m["text"])
    if k:
        km = float(k.group(1).replace(",", "."))
    elif m["laenge_m"]:
        try:
            km = round(float(m["laenge_m"]) / 1000, 1)
        except ValueError:
            pass
    minuten = None
    s = re.search(r"(\d+)\s*(?:Std|Stunde)n?(?:\s*(\d+)\s*Min)?", m["text"])
    mm = re.search(r"(\d+)\s*Min", m["text"])
    if s:
        minuten = int(s.group(1)) * 60 + int(s.group(2) or 0)
    elif mm:
        minuten = int(mm.group(1))
    elif m["verzoegerung_s"]:
        try:
            minuten = round(float(m["verzoegerung_s"]) / 60)
        except ValueError:
            pass
    return {"stau": True, "ab": ab, "bis": bis, "km": km, "minuten": minuten, "text": m["text"]}


def main():
    debug = "--debug" in sys.argv
    key = os.environ.get("OTD_KEY", "").strip()
    jetzt = datetime.now(timezone.utc).isoformat(timespec="seconds")
    res = {"checked": jetzt, "ok": False, "fehler": None,
           "sued": {"stau": False}, "nord": {"stau": False}, "tunnel": None, "weitere": []}
    try:
        if not key:
            raise RuntimeError("Kein Token (OTD_KEY) hinterlegt")
        meldungen = auswerten(holen(key), debug)
        res["ok"] = True
        for m in meldungen:
            t = m["text"] + " " + m["orte"]
            ist_stau = m["typ"] == "AbnormalTraffic" or bool(STAU_RE.search(m["text"]))
            if re.search(r"Gotthard.{0,30}(Strassen)?tunnel.{0,60}gesperrt|Tunnel.{0,40}Gotthard.{0,60}gesperrt", t, re.I):
                res["tunnel"] = m["text"]
            if ist_stau and SUED_RE.search(t) and not res["sued"]["stau"]:
                res["sued"] = stau_aus(m)
            elif ist_stau and NORD_RE.search(t) and not res["nord"]["stau"]:
                res["nord"] = stau_aus(m)
            elif m["text"]:
                res["weitere"].append(m["text"])
        res["weitere"] = list(dict.fromkeys(res["weitere"]))[:6]
        print(f"{len(meldungen)} Uri-Meldungen, Stau Sued: {res['sued']}, Nord: {res['nord']}")
    except urllib.error.HTTPError as e:
        res["fehler"] = f"HTTP {e.code}"
        print("HTTP-Fehler", e.code, e.read()[:300])
    except Exception as e:  # noqa: BLE001
        res["fehler"] = str(e)[:200]
        print("Fehler:", e)

    # Nur schreiben, wenn sich der Inhalt geaendert hat oder der letzte Stand
    # aelter als 60 Min. ist (sonst 96 Commits pro Tag).
    alt = {}
    if OUT.exists():
        try:
            alt = json.loads(OUT.read_text(encoding="utf-8"))
        except ValueError:
            alt = {}
    vergleich = lambda d: {k: v for k, v in d.items() if k != "checked"}
    alt_zeit = alt.get("checked")
    zu_alt = True
    if alt_zeit:
        zu_alt = datetime.now(timezone.utc) - datetime.fromisoformat(alt_zeit) > timedelta(minutes=55)
    if vergleich(alt) != vergleich(res) or zu_alt:
        OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        print("geschrieben")
    else:
        print("unveraendert")


if __name__ == "__main__":
    main()
