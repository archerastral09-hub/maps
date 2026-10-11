#!/usr/bin/env python3
"""
Extrait de data/resultats_*.jsonl tous les établissements dont la catégorie principale (champ « category »)
est « Agence immobilière » ou « Agent immobilier ». Tout le reste est écarté.

Sortie : export/agences_immobilieres.csv  (séparateur ;, UTF-8 avec BOM, ouvrable directement dans Excel)
Colonnes : Nom ; Adresse ; Catégorie ; Site web ; Téléphone ; Horaires ; Source

Réglages (variables d'environnement) :
  DATA_DIR            dossier des résultats (défaut : data)
  OUT_FILE            fichier de sortie (défaut : export/agences_immobilieres.csv)
  EXTRA_CATEGORIES    catégories supplémentaires à garder, séparées par « | »
                      ex. "Agence de location immobilière|Agence immobilière commerciale"
"""
import csv
import json
import os
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
OUT_FILE = Path(os.getenv("OUT_FILE", "export/agences_immobilieres.csv"))
DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def fold(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


KEEP = {"agence immobiliere": "Agence immobilière", "agent immobilier": "Agent immobilier"}
for extra in filter(None, (x.strip() for x in os.getenv("EXTRA_CATEGORIES", "").split("|"))):
    KEEP[fold(extra)] = extra


def iter_jsonl(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    pass


def fmt_phone(raw):
    d = re.sub(r"\D", "", raw or "")
    if d.startswith("33") and len(d) == 11:
        d = "0" + d[2:]
    if len(d) == 10 and d.startswith("0"):
        return " ".join(d[i:i + 2] for i in range(0, 10, 2))   # avec espaces : Excel garde le 0 initial
    return (raw or "").strip()


def fmt_hours(h):
    if not h:
        return ""
    if isinstance(h, str):
        return h
    by_day = {}
    for e in h:
        if isinstance(e, dict):
            by_day[str(e.get("day") or "").lower()] = ", ".join(str(x) for x in (e.get("hours") or []))
    parts = [f"{d}: {by_day[d]}" for d in DAYS if d in by_day]
    parts += [f"{d}: {v}" for d, v in by_day.items() if d not in DAYS]
    return " | ".join(parts)


def source_link(p):
    """Lien vers la fiche Google Maps de l'établissement."""
    if p.get("placeId"):
        return f"https://www.google.com/maps/search/?api=1&query=Google&query_place_id={p['placeId']}"
    m = re.search(r":(0x[0-9a-fA-F]+)$", p.get("cid") or p.get("featureId") or "")
    if m:
        return f"https://www.google.com/maps?cid={int(m.group(1), 16)}"
    return ""


def completeness(row):
    return sum(bool(row[k]) for k in ("Site web", "Téléphone", "Horaires"))


def main():
    files = sorted(DATA_DIR.glob("resultats_*.jsonl"))
    scanned = no_cat = dupes = 0
    kept = {}
    dropped = Counter()
    for path in files:
        for rec in iter_jsonl(path):
            for p in rec.get("places") or []:
                scanned += 1
                cat = (p.get("category") or "").strip()
                if not cat:
                    no_cat += 1
                    continue
                label = KEEP.get(fold(cat))
                if not label:
                    dropped[cat] += 1
                    continue
                row = {
                    "Nom": (p.get("title") or "").strip(),
                    "Adresse": (p.get("address") or "").strip(),
                    "Catégorie": label,
                    "Site web": (p.get("website") or "").strip(),
                    "Téléphone": fmt_phone(p.get("phoneNumber")),
                    "Horaires": fmt_hours(p.get("hours")),
                    "Source": source_link(p),
                }
                key = p.get("placeId") or p.get("cid") or f"{fold(row['Nom'])}|{fold(row['Adresse'])}"
                if key in kept:
                    dupes += 1
                    if completeness(row) > completeness(kept[key]):   # on garde la fiche la plus complète
                        kept[key] = row
                else:
                    kept[key] = row

    rows = sorted(kept.values(), key=lambda r: (fold(r["Nom"]), fold(r["Adresse"])))
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    cols = ["Nom", "Adresse", "Catégorie", "Site web", "Téléphone", "Horaires", "Source"]
    with open(OUT_FILE, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter=";")
        w.writeheader()
        w.writerows(rows)

    by_cat = Counter(r["Catégorie"] for r in rows)
    near = [(c, n) for c, n in dropped.most_common() if "immobili" in fold(c)]
    lines = [
        "## Export agences immobilières",
        "",
        f"- Fichiers lus : {len(files)} · résultats Google examinés : {scanned:,}".replace(",", " "),
        f"- **Gardés : {len(rows):,}**".replace(",", " ") + " (" + ", ".join(f"{n:,} {c.lower()}".replace(",", " ") for c, n in by_cat.most_common()) + ")",
        f"- Écartés (autre catégorie) : {sum(dropped.values()):,} · sans catégorie : {no_cat:,} · doublons fusionnés : {dupes:,}".replace(",", " "),
        f"- Avec téléphone : {sum(bool(r['Téléphone']) for r in rows):,} · avec site : {sum(bool(r['Site web']) for r in rows):,} · avec horaires : {sum(bool(r['Horaires']) for r in rows):,}".replace(",", " "),
    ]
    if near:
        lines += ["", "Catégories « immobilier » écartées (à ajouter via EXTRA_CATEGORIES si tu les veux) :", ""]
        lines += [f"- {c} : {n:,}".replace(",", " ") for c, n in near[:10]]
    text = "\n".join(lines) + "\n"
    print(text)
    (OUT_FILE.parent / "export_resume.md").write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
