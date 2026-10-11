#!/usr/bin/env python3
"""
Compte rendu complet des résultats Searlo (data/resultats_*.jsonl).

Aucune dépendance : bibliothèque standard uniquement.
Sorties (dossier rapport/) :
  rapport.md                 le compte rendu lisible (aussi affiché dans le résumé du job GitHub)
  stats.json                 les mêmes chiffres, exploitables par un programme
  agences_resume.csv         1 ligne par agence : meilleur résultat, niveau de correspondance, partages...
  resultats_detail.csv       1 ligne par (agence, résultat Google) avec tous les scores
  domaines_partages.csv      domaines utilisés par plusieurs agences (réseaux, franchises)
  placeid_partages.csv       fiches Google trouvées par plusieurs agences (doublons déguisés)
  telephones_partages.csv    téléphones utilisés par plusieurs agences
  agences_a_verifier.csv     agences dont le résultat est douteux, fermé, ou en erreur

Réglages (variables d'environnement) : DATA_DIR, OUT_DIR, INPUT_FILE, NAME_STRONG, NAME_OK
"""
import csv
import difflib
import hashlib
import json
import os
import re
import statistics
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
OUT_DIR = Path(os.getenv("OUT_DIR", "rapport"))
INPUT_FILE = Path(os.getenv("INPUT_FILE", "data/agences_nettoyees.csv"))
NAME_STRONG = float(os.getenv("NAME_STRONG", "0.85"))   # nom "quasi identique"
NAME_OK = float(os.getenv("NAME_OK", "0.60"))           # nom "ressemblant"
TOP = 25
MAX_ATTEMPTS = 3                                        # même valeur que enrich_searlo.py

LEVELS = ["exact", "probable", "nom_seul", "adresse_seule", "aucun"]
LEVEL_LABEL = {
    "exact": f"Exact (nom ≥ {NAME_STRONG:.0%} + adresse concordante)",
    "probable": f"Probable (nom ≥ {NAME_OK:.0%} + adresse concordante)",
    "nom_seul": f"Nom seul (nom ≥ {NAME_OK:.0%}, adresse différente)",
    "adresse_seule": "Adresse seule (autre établissement au même endroit)",
    "aucun": "Aucun lien avec l'original",
}
RANK = {k: i for i, k in enumerate(LEVELS)}
RELIABLE = ("exact", "probable")   # correspondances fiables
RETAINED = ("exact", "probable", "nom_seul")

NAME_STOP = {
    "agence", "agences", "immobilier", "immobiliere", "immobiliers", "immobilieres", "immo",
    "a", "de", "du", "des", "la", "le", "les", "et", "en", "au", "aux", "d", "l", "sarl", "sas", "sa",
}
ADDR_STOP = {
    "rue", "avenue", "av", "boulevard", "bd", "place", "chemin", "impasse", "allee", "route", "cours", "quai",
    "de", "la", "le", "les", "du", "des", "d", "l", "et", "en", "au", "aux", "france",
}
SOCIAL = {"facebook.com", "fb.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "youtube.com",
          "tiktok.com", "pinterest.com", "linktr.ee", "wa.me", "whatsapp.com"}
PORTAILS = {"pagesjaunes.fr", "mappy.com", "yelp.com", "yelp.fr", "seloger.com", "leboncoin.fr", "logic-immo.com",
            "bienici.com", "pap.fr", "meilleursagents.com", "societe.com", "kompass.com", "tripadvisor.fr",
            "tripadvisor.com", "google.com", "business.site", "sites.google.com", "118712.fr", "cylex.fr"}
IMMO_KW = ("immobili", "syndic", "promoteur", "gestionnaire de bien", "administrateur de bien", "lotisseur")


# ───────────────────────── utilitaires ─────────────────────────
def key_norm(s):
    return re.sub(r"\s+", " ", s or "").strip().casefold()


def row_key(nom, adresse):
    """Même identifiant que enrich_searlo.py (nom + adresse normalisés)."""
    return hashlib.sha1(f"{key_norm(nom)}|{key_norm(adresse)}".encode()).hexdigest()[:12]


def fold(s):
    """Minuscules, sans accents ni ponctuation (pour comparer)."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def fmt(n):
    return f"{n:,}".replace(",", " ")


def pct(n, d):
    return f"{100 * n / d:.1f} %" if d else "–"


def table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c).replace("|", "\\|") for c in r) + " |")
    return "\n".join(out)


def counter_table(c, total, label, top=None, order=None):
    items = [(k, c[k]) for k in order if k in c] if order else c.most_common(top)
    return table([label, "Nombre", "%"], [(k, fmt(v), pct(v, total)) for k, v in items])


def iter_jsonl(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    pass


def get_domain(p):
    d = (p.get("domain") or "").strip().lower()
    if not d and p.get("website"):
        try:
            d = urlparse(p["website"]).netloc.lower()
        except ValueError:
            d = ""
    return d[4:] if d.startswith("www.") else d


def domain_type(domain):
    if not domain:
        return "Aucun site"
    for s in SOCIAL:
        if domain == s or domain.endswith("." + s):
            return "Réseau social"
    for s in PORTAILS:
        if domain == s or domain.endswith("." + s):
            return "Annuaire / portail"
    return "Site propre"


def norm_phone(p):
    d = re.sub(r"\D", "", p or "")
    if d.startswith("33") and len(d) == 11:
        d = "0" + d[2:]
    return d


def phone_type(d):
    if not d:
        return "Aucun"
    if not (len(d) == 10 and d.startswith("0")):
        return "Format inhabituel"
    return {"6": "Mobile (06/07)", "7": "Mobile (06/07)", "9": "VoIP (09)", "8": "Numéro spécial (08)"}.get(d[1], "Fixe (01-05)")


def famille(cat):
    c = fold(cat)
    if not c:
        return "Sans catégorie"
    if "agent immobilier" in c:
        return "Agent immobilier"
    if "agence" in c and "immobili" in c:
        return "Agence immobilière"
    if any(k in c for k in IMMO_KW):
        return "Autre métier de l'immobilier"
    return "Hors immobilier"


def dept_of(address):
    cps = re.findall(r"\b(\d{5})\b", address or "")
    if not cps:
        return None, None, None
    cp = cps[-1]
    dept = cp[:3] if cp[:2] in ("97", "98") else cp[:2]
    m = re.search(rf"\b{cp}\s+([^,]+)", address)
    return cp, dept, (m.group(1).strip() if m else None)


# ───────────────────────── correspondance avec l'original ─────────────────────────
def orig_info(nom, adresse):
    core = re.split(r"\s+[-–—|]\s+", (nom or "").strip())[0]
    segs = [s.strip() for s in re.split(r"[,;]", adresse or "") if s.strip()]
    city_segs = [s for s in segs if not re.search(r"\d", s) and fold(s) not in ("", "france")]
    city_tok = set()
    for s in city_segs:
        city_tok |= set(fold(s).split())
    words = fold(adresse).split()
    cps = re.findall(r"\b\d{5}\b", adresse or "")
    has_number = any(re.fullmatch(r"\d{1,4}", w) for w in words)
    if not words:
        adr_type = "Adresse vide"
    elif has_number:
        adr_type = "Adresse précise (n° de rue)"
    elif cps:
        adr_type = "Code postal / ville"
    else:
        adr_type = "Ville seule (ou département)"
    return {
        "core": core, "city_segs": city_segs, "city_tok": city_tok, "cp": cps[0] if cps else None,
        "toks": [w for w in words if w not in ADDR_STOP], "adr_type": adr_type,
    }


def name_tokens(s, drop):
    toks = [t for t in fold(s).split() if t not in NAME_STOP]
    kept = {t for t in toks if t not in drop}
    return kept or set(toks) or set(fold(s).split())


def name_score(nom, title, info):
    o = name_tokens(info["core"] if info["core"] else nom, info["city_tok"])
    t = name_tokens(title, info["city_tok"])
    if not o or not t:
        return 0.0
    inter = len(o & t)
    cont = inter / len(o)
    dice = 2 * inter / (len(o) + len(t))
    seq = difflib.SequenceMatcher(None, " ".join(sorted(o)), " ".join(sorted(t))).ratio()
    return round(max(seq, 0.5 * cont + 0.5 * dice), 3)


def addr_check(info, place_addr):
    pa = fold(place_addr)
    ptoks = set(pa.split())
    padded = f" {pa} "
    cp_match = (info["cp"] in ptoks) if info["cp"] else None
    ville_match = any(f" {fold(s)} " in padded for s in info["city_segs"])
    toks = info["toks"]
    adr_score = round(sum(t in ptoks for t in toks) / len(toks), 3) if toks else 0.0
    ok = bool(cp_match) or adr_score >= 0.8 or ville_match
    return ok, cp_match, ville_match, adr_score


def level_of(ns, addr_ok):
    if ns >= NAME_STRONG and addr_ok:
        return "exact"
    if ns >= NAME_OK and addr_ok:
        return "probable"
    if ns >= NAME_OK:
        return "nom_seul"
    if addr_ok:
        return "adresse_seule"
    return "aucun"


def analyze_record(rec):
    nom, adresse = rec.get("nom") or "", rec.get("adresse") or ""
    info = orig_info(nom, adresse)
    places = []
    for i, p in enumerate(rec.get("places") or [], 1):
        title, address = p.get("title") or "", p.get("address") or ""
        ns = name_score(nom, title, info)
        ok, cp_m, ville_m, adr_s = addr_check(info, address)
        dom = get_domain(p)
        cats = [c for c in (p.get("categories") or []) if c]
        cat = p.get("category") or (cats[0] if cats else "")
        attrs = []
        if isinstance(p.get("attributes"), dict):
            for sec, d in p["attributes"].items():
                if isinstance(d, dict):
                    attrs += [f"{sec}.{k}" for k, v in d.items() if v is True]
        cp, dept, city = dept_of(address)
        ph = norm_phone(p.get("phoneNumber"))
        places.append({
            "pos": p.get("position") or i, "title": title, "address": address, "category": cat, "categories": cats,
            "famille": famille(cat), "immo_any": any(famille(c) not in ("Hors immobilier", "Sans catégorie") for c in cats + [cat]),
            "both_agence_agent": any(famille(c) == "Agent immobilier" for c in cats) and any(famille(c) == "Agence immobilière" for c in cats),
            "phone": ph, "phone_type": phone_type(ph), "website": p.get("website") or "", "domain": dom,
            "dtype": domain_type(dom), "rating": p.get("rating"), "rc": p.get("ratingCount"),
            "bstatus": p.get("businessStatus") or "inconnu", "open_now": p.get("openNow"),
            "has_hours": bool(p.get("hours")), "price": p.get("priceLevel"), "thumb": bool(p.get("thumbnailUrl")),
            "opening_date": p.get("openingDate") or p.get("openingDateText"), "attrs": attrs,
            "lat": p.get("latitude"), "lon": p.get("longitude"),
            "pid": p.get("placeId") or p.get("cid") or "", "cp": cp, "dept": dept, "city": city,
            "name_score": ns, "nom_identique": fold(info["core"]) == fold(title) or fold(nom) == fold(title),
            "addr_ok": ok, "cp_match": cp_m, "ville_match": ville_m, "adr_score": adr_s,
            "level": level_of(ns, ok),
        })
    best = min(places, key=lambda p: (RANK[p["level"]], -p["name_score"], p["pos"])) if places else None
    return {
        "key": row_key(nom, adresse), "id": rec.get("id"), "nom": nom, "adresse": adresse,
        "status": rec.get("status") or ("ok" if places else "empty"), "lite": rec.get("lite"),
        "nb": len(places), "adr_type": info["adr_type"], "places": places, "best": best,
        "level": best["level"] if best else "vide", "fetched_at": rec.get("fetched_at") or "",
        "api_key": rec.get("key") or "",
        "n_match": sum(p["level"] in RELIABLE for p in places),
        "dom_repeat": len({p["domain"] for p in places if p["domain"]}) < len([p for p in places if p["domain"]]),
        "pid_repeat": len({p["pid"] for p in places if p["pid"]}) < len([p for p in places if p["pid"]]),
    }


# ───────────────────────── lecture des données ─────────────────────────
def load_results():
    agences, lines = {}, 0
    for path in sorted(DATA_DIR.glob("resultats_*.jsonl")):
        for rec in iter_jsonl(path):
            if not rec.get("nom"):
                continue
            lines += 1
            a = analyze_record(rec)
            agences[a["key"]] = a      # la dernière version d'une agence l'emporte
    return agences, lines


def load_csv_keys():
    if not INPUT_FILE.exists():
        return None
    keys, rows, empty_name = set(), 0, 0
    with open(INPUT_FILE, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f, delimiter=";"):
            nom, adr = (r.get("Nom") or "").strip(), (r.get("Adresse") or "").strip()
            if not nom:
                empty_name += 1
                continue
            rows += 1
            keys.add(row_key(nom, adr))
    return {"lignes": rows, "uniques": keys, "sans_nom": empty_name}


def load_errors():
    path = DATA_DIR / "erreurs.jsonl"
    per, codes = Counter(), Counter()
    if path.exists():
        for rec in iter_jsonl(path):
            if rec.get("final") and rec.get("nom"):
                per[row_key(rec["nom"], rec.get("adresse"))] += 1
                codes[(rec.get("http_status"), rec.get("code") or "")] += 1
    return per, codes


def load_state():
    p = DATA_DIR / "state.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_csv(path, header, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(header)
        w.writerows(rows)


# ───────────────────────── rapport ─────────────────────────
def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    agences, lines = load_results()
    if not agences:
        print(f"Aucun résultat trouvé dans {DATA_DIR}/resultats_*.jsonl", file=sys.stderr)
        (OUT_DIR / "rapport.md").write_text("# Rapport Searlo\n\nAucun résultat disponible pour l'instant.\n", encoding="utf-8")
        return 0
    ag = list(agences.values())
    N = len(ag)
    st = {}
    md = []

    def H(t):
        md.append(f"\n## {t}\n")

    md.append(f"# Rapport Searlo — {datetime.now(timezone.utc):%d/%m/%Y %H:%M} UTC\n")
    md.append(f"{fmt(N)} agences analysées. Les seuils de correspondance sont : nom ≥ {NAME_STRONG:.0%} = quasi identique, "
              f"≥ {NAME_OK:.0%} = ressemblant. « Fiable » = niveau exact ou probable.")

    # ── 1. Avancement ──
    H("1. Avancement")
    csv_info, err_per, err_codes, state = load_csv_keys(), *load_errors(), load_state()
    st["agences_traitees"] = N
    rows1 = [("Agences traitées (résultats uniques)", fmt(N)),
             ("Lignes de résultats lues (doublons de relance inclus)", fmt(lines))]
    if csv_info:
        uniq = csv_info["uniques"]
        done = sum(1 for k in agences if k in uniq)
        abandoned = sum(1 for k, c in err_per.items() if c >= MAX_ATTEMPTS and k not in agences and k in uniq)
        remaining = len(uniq) - done
        rows1 = [("Lignes du CSV d'entrée", fmt(csv_info["lignes"])),
                 ("Agences uniques (après doublons nom + adresse)", fmt(len(uniq))),
                 ("Doublons du CSV ignorés", fmt(csv_info["lignes"] - len(uniq))),
                 ("Agences traitées", f"{fmt(done)} ({pct(done, len(uniq))})"),
                 ("Agences restantes", fmt(remaining)),
                 ("dont abandonnées après trop d'échecs", fmt(abandoned))] + rows1[1:]
        st.update(csv_uniques=len(uniq), restantes=remaining, abandonnees=abandoned)
        runs = state.get("runs") or []
        mins = 0.0
        tr = 0
        for r in runs:
            try:
                mins += (datetime.fromisoformat(r["end"]) - datetime.fromisoformat(r["start"])).total_seconds() / 60
                tr += r.get("traitees", 0)
            except (KeyError, ValueError):
                pass
        if mins > 0 and tr > 0 and remaining > 0:
            h = remaining / (tr / mins) / 60
            rows1.append(("Temps restant estimé", f"≈ {h:.1f} h (≈ {tr / mins * 60:.0f} agences/h)"))
    md.append(table(["Indicateur", "Valeur"], rows1))
    if err_codes:
        md.append("\nErreurs définitives par code :\n")
        md.append(table(["HTTP", "Code", "Nombre"], [(h, c, fmt(n)) for (h, c), n in err_codes.most_common(10)]))

    # ── 2. Résultats par agence ──
    H("2. Nombre de résultats par agence")
    nb_c = Counter(a["nb"] for a in ag)
    md.append(counter_table(Counter({f"{k} résultat(s)" if k else "0 (vide)": v for k, v in nb_c.items()}), N, "Résultats renvoyés",
                            order=[f"{k} résultat(s)" if k else "0 (vide)" for k in sorted(nb_c)]))
    total_places = sum(a["nb"] for a in ag)
    md.append(f"\nVides : **{fmt(nb_c.get(0, 0))}** ({pct(nb_c.get(0, 0), N)}). Résultats Google au total : {fmt(total_places)} "
              f"(moyenne {total_places / N:.2f} par agence).")
    with_res = [a for a in ag if a["places"]]
    st.update(vides=nb_c.get(0, 0), nb_resultats={str(k): v for k, v in nb_c.items()}, resultats_total=total_places)
    lite = sum(1 for a in ag if a["lite"])
    if lite:
        md.append(f"\n{fmt(lite)} agences ont été récupérées en mode « lite » (champs possiblement réduits).")

    # ── 3. Correspondance avec l'original ──
    H("3. Correspondance avec le nom et l'adresse d'origine")
    lv = Counter(a["level"] for a in ag)
    md.append("Meilleur résultat de chaque agence :\n")
    md.append(table(["Niveau", "Agences", "%"],
                    [("Vide (aucun résultat)", fmt(lv.get("vide", 0)), pct(lv.get("vide", 0), N))] +
                    [(LEVEL_LABEL[k], fmt(lv.get(k, 0)), pct(lv.get(k, 0), N)) for k in LEVELS]))
    fiables = sum(lv.get(k, 0) for k in RELIABLE)
    md.append(f"\n**Correspondances fiables : {fmt(fiables)} ({pct(fiables, N)})**, soit {pct(fiables, len(with_res))} des agences qui ont au moins un résultat.")
    st["niveaux"] = dict(lv)

    md.append("\nNiveau selon le nombre de résultats renvoyés :\n")
    cross = defaultdict(Counter)
    for a in with_res:
        cross[a["nb"]][a["level"]] += 1
    md.append(table(["Résultats", "Exact", "Probable", "Nom seul", "Adresse seule", "Aucun"],
                    [(k, *[fmt(cross[k].get(l, 0)) for l in LEVELS]) for k in sorted(cross)]))

    md.append("\nPosition du meilleur résultat dans la liste Google (agences avec correspondance exacte, probable ou nom seul) :\n")
    pos = Counter(("1er" if a["best"]["pos"] == 1 else "2e" if a["best"]["pos"] == 2 else "3e ou plus") for a in with_res
                  if a["level"] in RETAINED)
    md.append(counter_table(pos, sum(pos.values()), "Position", order=["1er", "2e", "3e ou plus"]))

    multi = [a for a in with_res if a["nb"] >= 2]
    nm = Counter(a["n_match"] for a in multi)
    md.append(f"\nAgences avec **plusieurs résultats** : {fmt(len(multi))}. Combien de ces résultats correspondent (fiable) à l'original :\n")
    md.append(counter_table(Counter({f"{k} résultat(s) fiable(s)": v for k, v in nm.items()}), len(multi), "Correspondances",
                            order=[f"{k} résultat(s) fiable(s)" for k in sorted(nm)]))
    md.append(f"\nMême domaine répété dans les résultats d'une même agence : {fmt(sum(a['dom_repeat'] for a in multi))} · "
              f"même fiche Google répétée : {fmt(sum(a['pid_repeat'] for a in multi))}.")

    best_ns = [a["best"]["name_score"] for a in with_res]
    buckets = Counter()
    for s in best_ns:
        buckets["0 – 20 %" if s < .2 else "20 – 40 %" if s < .4 else "40 – 60 %" if s < .6 else "60 – 85 %" if s < .85 else "85 – 100 %"] += 1
    md.append("\nScore de ressemblance du nom (meilleur résultat) :\n")
    md.append(counter_table(buckets, len(with_res), "Score", order=["0 – 20 %", "20 – 40 %", "40 – 60 %", "60 – 85 %", "85 – 100 %"]))
    md.append(f"\nNom strictement identique (hors accents/majuscules) : {fmt(sum(a['best']['nom_identique'] for a in with_res))}.")

    md.append("\nType d'adresse d'origine et concordance :\n")
    adr = Counter(a["adr_type"] for a in ag)
    adr_ok = Counter(a["adr_type"] for a in with_res if a["best"]["addr_ok"])
    md.append(table(["Adresse d'origine", "Agences", "dont adresse concordante (meilleur résultat)"],
                    [(k, fmt(v), f"{fmt(adr_ok.get(k, 0))} ({pct(adr_ok.get(k, 0), sum(1 for a in with_res if a['adr_type'] == k))})")
                     for k, v in adr.most_common()]))
    cp_known = [a for a in with_res if a["best"]["cp_match"] is not None]
    md.append(f"\nCode postal d'origine retrouvé dans l'adresse du résultat : {fmt(sum(a['best']['cp_match'] for a in cp_known))} sur {fmt(len(cp_known))} agences avec un code postal d'origine.")

    allp = [p for a in ag for p in a["places"]]
    md.append(f"\nAu niveau de **tous les résultats** ({fmt(len(allp))}) : nom ressemblant {fmt(sum(p['name_score'] >= NAME_OK for p in allp))} "
              f"({pct(sum(p['name_score'] >= NAME_OK for p in allp), len(allp))}) · adresse concordante {fmt(sum(p['addr_ok'] for p in allp))} "
              f"({pct(sum(p['addr_ok'] for p in allp), len(allp))}) · les deux {fmt(sum(p['level'] in RELIABLE for p in allp))} "
              f"({pct(sum(p['level'] in RELIABLE for p in allp), len(allp))}).")
    st["resultats_nom_ok"] = sum(p["name_score"] >= NAME_OK for p in allp)
    st["resultats_adresse_ok"] = sum(p["addr_ok"] for p in allp)

    # populations pour la suite
    rel = [a for a in with_res if a["level"] in RELIABLE]
    R = len(rel)

    # ── 4. Types d'établissement ──
    H("4. Types d'établissement")
    fam_all = Counter(a["best"]["famille"] for a in with_res)
    fam_rel = Counter(a["best"]["famille"] for a in rel)
    order = ["Agence immobilière", "Agent immobilier", "Autre métier de l'immobilier", "Hors immobilier", "Sans catégorie"]
    md.append(table(["Catégorie principale du meilleur résultat", "Toutes agences avec résultat", "Correspondances fiables"],
                    [("Vide (aucun résultat)", fmt(nb_c.get(0, 0)), "–")] +
                    [(k, f"{fmt(fam_all.get(k, 0))} ({pct(fam_all.get(k, 0), len(with_res))})",
                      f"{fmt(fam_rel.get(k, 0))} ({pct(fam_rel.get(k, 0), R)})") for k in order]))
    md.append(f"\nMeilleur résultat avec un métier de l'immobilier dans **n'importe quelle** de ses catégories : "
              f"{fmt(sum(a['best']['immo_any'] for a in with_res))} ({pct(sum(a['best']['immo_any'] for a in with_res), len(with_res))}). "
              f"Fiches à la fois « Agence » et « Agent » immobilier : {fmt(sum(a['best']['both_agence_agent'] for a in with_res))}.")
    md.append("\nCatégories principales les plus fréquentes (correspondances fiables) :\n")
    md.append(counter_table(Counter(a["best"]["category"] or "(vide)" for a in rel), R, "Catégorie", top=TOP))
    cat_any = Counter(c for a in rel for c in a["best"]["categories"])
    md.append("\nCatégories secondaires les plus fréquentes (toutes catégories des fiches fiables) :\n")
    md.append(table(["Catégorie", "Fiches"], [(k, fmt(v)) for k, v in cat_any.most_common(TOP)]))
    st["familles_fiables"] = dict(fam_rel)
    st["familles_toutes"] = dict(fam_all)

    # ── 5. Sites, domaines, doublons ──
    H("5. Sites web, domaines partagés et doublons déguisés")
    md.append(f"Population : {fmt(R)} correspondances fiables.\n")
    dt = Counter(a["best"]["dtype"] for a in rel)
    md.append(counter_table(dt, R, "Type de site", order=["Site propre", "Réseau social", "Annuaire / portail", "Aucun site"]))

    shared = {}
    for label, getter in (("domain", lambda p: p["domain"] if p["dtype"] == "Site propre" else ""),
                          ("pid", lambda p: p["pid"]), ("phone", lambda p: p["phone"])):
        d = defaultdict(list)
        for a in rel:
            v = getter(a["best"])
            if v:
                d[v].append(a)
        shared[label] = d
    # compteurs de partage mis dans les lignes
    for a in ag:
        b = a["best"]
        a["dom_n"] = len(shared["domain"].get(b["domain"], [])) if b and a["level"] in RELIABLE and b["dtype"] == "Site propre" else 0
        a["pid_n"] = len(shared["pid"].get(b["pid"], [])) if b and a["level"] in RELIABLE else 0
        a["tel_n"] = len(shared["phone"].get(b["phone"], [])) if b and a["level"] in RELIABLE else 0

    dom = shared["domain"]
    sizes = Counter()
    for v in dom.values():
        n = len(v)
        sizes["1 agence (domaine unique)" if n == 1 else "2 agences" if n == 2 else "3 à 5 agences" if n <= 5 else "6 à 10 agences" if n <= 10 else "11 agences ou plus"] += 1
    md.append(f"\n**Domaines distincts : {fmt(len(dom))}** pour {fmt(dt.get('Site propre', 0))} agences avec site propre.\n")
    md.append(counter_table(sizes, len(dom), "Un domaine est utilisé par", order=["1 agence (domaine unique)", "2 agences", "3 à 5 agences", "6 à 10 agences", "11 agences ou plus"]))
    multi_dom = {k: v for k, v in dom.items() if len(v) >= 2}
    md.append(f"\nDomaines partagés par ≥ 2 agences : {fmt(len(multi_dom))}, couvrant {fmt(sum(len(v) for v in multi_dom.values()))} agences. Top {TOP} :\n")
    md.append(table(["Domaine", "Agences", "Exemples"],
                    [(k, len(v), " · ".join(x["nom"][:40] for x in v[:2]))
                     for k, v in sorted(multi_dom.items(), key=lambda kv: -len(kv[1]))[:TOP]]))
    write_csv(OUT_DIR / "domaines_partages.csv", ["domaine", "nb_agences", "agences"],
              [(k, len(v), " || ".join(x["nom"] for x in v[:20])) for k, v in sorted(multi_dom.items(), key=lambda kv: -len(kv[1]))])

    pid = {k: v for k, v in shared["pid"].items() if len(v) >= 2}
    extra = sum(len(v) - 1 for v in pid.values())
    md.append(f"\n**Même fiche Google trouvée par plusieurs agences : {fmt(len(pid))} fiches**, concernant {fmt(sum(len(v) for v in pid.values()))} agences "
              f"(→ {fmt(extra)} agences en trop si ce sont des doublons du CSV, détail dans `placeid_partages.csv`).")
    write_csv(OUT_DIR / "placeid_partages.csv", ["placeId", "nb_agences", "titre_fiche", "agences"],
              [(k, len(v), v[0]["best"]["title"], " || ".join(f"{x['nom']} [{x['adresse']}]" for x in v[:20]))
               for k, v in sorted(pid.items(), key=lambda kv: -len(kv[1]))])
    ph = {k: v for k, v in shared["phone"].items() if len(v) >= 2}
    md.append(f"\nMême téléphone pour plusieurs agences : {fmt(len(ph))} numéros, {fmt(sum(len(v) for v in ph.values()))} agences concernées.")
    write_csv(OUT_DIR / "telephones_partages.csv", ["telephone", "nb_agences", "agences"],
              [(k, len(v), " || ".join(x["nom"] for x in v[:20])) for k, v in sorted(ph.items(), key=lambda kv: -len(kv[1]))])
    st.update(domaines_distincts=len(dom), domaines_partages=len(multi_dom), placeid_partages=len(pid), telephones_partages=len(ph))

    # ── 6. Qualité des fiches ──
    H("6. Qualité des fiches (correspondances fiables)")
    B = [a["best"] for a in rel]
    md.append(table(["Information", "Fiches", "%"], [
        ("Téléphone présent", fmt(sum(1 for b in B if b["phone"])), pct(sum(1 for b in B if b["phone"]), R)),
        ("Site web présent", fmt(sum(1 for b in B if b["website"])), pct(sum(1 for b in B if b["website"]), R)),
        ("Téléphone ou site", fmt(sum(1 for b in B if b["phone"] or b["website"])), pct(sum(1 for b in B if b["phone"] or b["website"]), R)),
        ("Téléphone ET site", fmt(sum(1 for b in B if b["phone"] and b["website"])), pct(sum(1 for b in B if b["phone"] and b["website"]), R)),
        ("Note (rating)", fmt(sum(1 for b in B if b["rating"] is not None)), pct(sum(1 for b in B if b["rating"] is not None), R)),
        ("Horaires", fmt(sum(b["has_hours"] for b in B)), pct(sum(b["has_hours"] for b in B), R)),
        ("Photo (miniature)", fmt(sum(b["thumb"] for b in B)), pct(sum(b["thumb"] for b in B), R)),
        ("Date d'ouverture renseignée", fmt(sum(1 for b in B if b["opening_date"])), pct(sum(1 for b in B if b["opening_date"]), R)),
        ("Niveau de prix renseigné", fmt(sum(1 for b in B if b["price"] is not None)), pct(sum(1 for b in B if b["price"] is not None), R)),
    ]))
    md.append("\nType de téléphone :\n")
    md.append(counter_table(Counter(b["phone_type"] for b in B), R, "Type"))
    md.append("\nStatut de l'établissement :\n")
    md.append(counter_table(Counter(b["bstatus"] for b in B), R, "Statut"))
    rt = [b["rating"] for b in B if isinstance(b["rating"], (int, float))]
    if rt:
        rb = Counter("< 3" if x < 3 else "3 à 3,9" if x < 4 else "4 à 4,4" if x < 4.5 else "4,5 et +" for x in rt)
        md.append(f"\nNotes : moyenne **{statistics.mean(rt):.2f}**, médiane {statistics.median(rt):.1f}.\n")
        md.append(counter_table(rb, len(rt), "Note", order=["< 3", "3 à 3,9", "4 à 4,4", "4,5 et +"]))
    rc = [b["rc"] for b in B if isinstance(b["rc"], (int, float))]
    if rc:
        cb = Counter("1 à 9" if x < 10 else "10 à 49" if x < 50 else "50 à 199" if x < 200 else "200 et +" for x in rc if x > 0)
        md.append(f"\nNombre d'avis : total {fmt(int(sum(rc)))}, moyenne {statistics.mean(rc):.0f}, médiane {statistics.median(rc):.0f}. Sans avis : {fmt(sum(1 for x in rc if x == 0))}.\n")
        md.append(counter_table(cb, sum(cb.values()), "Avis", order=["1 à 9", "10 à 49", "50 à 199", "200 et +"]))
    attr = Counter(x for b in B for x in b["attrs"])
    if attr:
        md.append("\nAttributs déclarés (cochés « vrai ») :\n")
        md.append(table(["Attribut", "Fiches"], [(k, fmt(v)) for k, v in attr.most_common(15)]))

    # ── 7. Géographie ──
    H("7. Géographie (correspondances fiables)")
    dp = Counter(b["dept"] for b in B if b["dept"])
    md.append(f"Départements représentés : {len(dp)}. Top 15 :\n")
    md.append(table(["Département", "Agences"], [(k, fmt(v)) for k, v in dp.most_common(15)]))
    ct = Counter(f"{b['city']} ({b['dept']})" for b in B if b["city"])
    md.append("\nVilles les plus représentées :\n")
    md.append(table(["Ville", "Agences"], [(k, fmt(v)) for k, v in ct.most_common(15)]))

    # ── 8. Exécution ──
    H("8. Exécution et clés API")
    keys_usage = state.get("keys_usage") or {}
    if keys_usage:
        exhausted = state.get("exhausted") or {}
        solde = sum(float(v.get("dernier_solde") or 0) for k, v in keys_usage.items() if k not in exhausted)
        md.append(f"Clés utilisées : {len(keys_usage)} · épuisées : {len(exhausted)} · requêtes cumulées : "
                  f"{fmt(sum(v.get('requetes', 0) for v in keys_usage.values()))} · solde connu des clés actives : {fmt(int(solde))} crédits.\n")
    runs = state.get("runs") or []
    if runs:
        md.append(table(["Début (UTC)", "Fin", "Issue", "Traitées", "Erreurs", "Requêtes", "Restantes"],
                        [(r.get("start", "")[:16], r.get("end", "")[11:16], r.get("outcome", ""), fmt(r.get("traitees", 0)),
                          r.get("erreurs", 0), fmt(r.get("requetes", 0)), fmt(r.get("restantes", 0))) for r in runs[-10:]]))
    days = Counter(a["fetched_at"][:10] for a in ag if a["fetched_at"])
    if days:
        md.append("\nAgences récupérées par jour :\n")
        md.append(table(["Jour", "Agences"], [(k, fmt(v)) for k, v in sorted(days.items())[-14:]]))

    # ── CSV détaillés ──
    cols = ["id", "nom", "adresse", "type_adresse", "statut", "nb_resultats", "niveau", "nb_resultats_fiables", "position_meilleur",
            "nom_score", "adresse_concordante", "cp_concordant", "ville_concordante",
            "titre", "adresse_resultat", "categorie", "famille", "telephone", "site", "domaine", "type_site", "note", "nb_avis",
            "statut_etablissement", "placeId", "latitude", "longitude", "nb_agences_meme_domaine", "nb_agences_meme_fiche",
            "nb_agences_meme_tel", "recupere_le"]
    rows = []
    for a in ag:
        b = a["best"] or {}
        rows.append([a["id"] or a["key"], a["nom"], a["adresse"], a["adr_type"], a["status"], a["nb"], a["level"], a["n_match"],
                     b.get("pos", ""), b.get("name_score", ""), b.get("addr_ok", ""), b.get("cp_match", ""), b.get("ville_match", ""),
                     b.get("title", ""), b.get("address", ""), b.get("category", ""), b.get("famille", ""), b.get("phone", ""),
                     b.get("website", ""), b.get("domain", ""), b.get("dtype", ""), b.get("rating", ""), b.get("rc", ""),
                     b.get("bstatus", ""), b.get("pid", ""), b.get("lat", ""), b.get("lon", ""), a["dom_n"], a["pid_n"], a["tel_n"], a["fetched_at"]])
    write_csv(OUT_DIR / "agences_resume.csv", cols, rows)

    write_csv(OUT_DIR / "resultats_detail.csv",
              ["id_agence", "nom_origine", "adresse_origine", "position", "titre", "adresse", "categorie", "categories", "telephone", "site",
               "domaine", "note", "nb_avis", "statut_etablissement", "nom_score", "nom_identique", "adresse_concordante",
               "cp_concordant", "ville_concordante", "score_adresse", "niveau", "placeId"],
              [[a["id"] or a["key"], a["nom"], a["adresse"], p["pos"], p["title"], p["address"], p["category"], " | ".join(p["categories"]),
                p["phone"], p["website"], p["domain"], p["rating"], p["rc"], p["bstatus"], p["name_score"], p["nom_identique"],
                p["addr_ok"], p["cp_match"], p["ville_match"], p["adr_score"], p["level"], p["pid"]] for a in ag for p in a["places"]])

    to_check = []
    for a in ag:
        b = a["best"]
        if a["nb"] > 0 and a["level"] in ("aucun", "adresse_seule"):
            to_check.append((a["nom"], a["adresse"], "résultats sans rapport avec le nom", b["title"], b["address"], a["level"]))
        elif b and b["bstatus"].upper().startswith("CLOSED") and a["level"] in RELIABLE:
            to_check.append((a["nom"], a["adresse"], f"établissement {b['bstatus']}", b["title"], b["address"], a["level"]))
    for k, c in err_per.items():
        if k not in agences and c >= MAX_ATTEMPTS:
            to_check.append((k, "", f"abandonnée après {c} échecs", "", "", "erreur"))
    write_csv(OUT_DIR / "agences_a_verifier.csv", ["nom", "adresse", "raison", "titre_resultat", "adresse_resultat", "niveau"], to_check)
    closed = sum(1 for a in rel if a["best"]["bstatus"].upper().startswith("CLOSED"))
    H("9. À vérifier")
    md.append(f"{fmt(len(to_check))} agences listées dans `agences_a_verifier.csv` : résultats sans rapport "
              f"({fmt(lv.get('aucun', 0) + lv.get('adresse_seule', 0))}), établissements fermés parmi les fiables ({fmt(closed)}), abandons.")
    md.append("\n\nFichiers : `agences_resume.csv`, `resultats_detail.csv`, `domaines_partages.csv`, `placeid_partages.csv`, "
              "`telephones_partages.csv`, `agences_a_verifier.csv`, `stats.json`.")

    st["fiables"] = fiables
    (OUT_DIR / "stats.json").write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / "rapport.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"Rapport écrit dans {OUT_DIR}/ ({fmt(N)} agences)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
