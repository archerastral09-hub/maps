#!/usr/bin/env python3
"""Récupère les membres de l'équipe de chaque agence Laforêt.

Entrée  : agences_laforet.xlsx (onglet « Agences », colonnes « ID agence »,
          « Nom agence », « Site internet »).
Sortie  : onglet « Contacts » du même fichier, une ligne par membre.
          Colonnes remplies : ID agence, SIREN (repris de l'onglet Agences),
          Nom, Prénom, Fonction, Source (= URL de la page agence).
          Téléphone / e-mail / LinkedIn restent vides : la page n'en publie pas.
Aussi   : erreurs_laforet_equipes.csv (agences en échec ou sans équipe).

Variables d'environnement facultatives :
    INPUT_FILE   (défaut agences_laforet.xlsx)
    DELAY        pause entre deux agences en secondes (défaut 1.0)
    LIMIT        ne traiter que les N premières agences (test)
"""
import csv
import os
import re
import sys
import time

import openpyxl
import requests
from bs4 import BeautifulSoup

INPUT_FILE = os.environ.get("INPUT_FILE", "agences_laforet.xlsx")
DELAY = float(os.environ.get("DELAY", "1.0"))
LIMIT = int(os.environ.get("LIMIT", "0") or 0)
ERRORS_FILE = "erreurs_laforet_equipes.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9",
}

CONTACT_HEADERS = [
    "ID agence", "SIREN", "Nom", "Prénom", "Fonction",
    "Téléphone professionnel", "Email professionnel", "LinkedIn", "Source",
]


def split_name(full):
    """« Jérôme POMEL » -> (« POMEL », « Jérôme »). Les mots tout en
    majuscules forment le nom, les autres le prénom."""
    tokens = full.split()
    prenom, nom = [], []
    for tok in tokens:
        letters = re.sub(r"[^A-Za-zÀ-ÿ]", "", tok)
        is_upper = letters != "" and letters == letters.upper()
        if is_upper or nom:  # dès le premier mot en capitales, tout le reste = nom
            nom.append(tok)
        else:
            prenom.append(tok)
    if not nom:  # aucun mot en capitales : dernier mot = nom
        if len(prenom) > 1:
            nom = [prenom.pop()]
        else:
            nom, prenom = prenom, []
    return " ".join(nom), " ".join(prenom)


def parse_team(html):
    """Renvoie [(nom_complet, fonction), ...] depuis le bloc « Notre équipe »."""
    soup = BeautifulSoup(html, "html.parser")
    title = next(
        (h for h in soup.find_all(["h2", "h1"])
         if re.search(r"[ée]quipe", h.get_text(" ", strip=True), re.I)),
        None,
    )
    if title is None:
        return []
    section = title.find_parent("div")
    # on remonte jusqu'à trouver un conteneur avec des <h3> (cartes membres)
    for _ in range(4):
        if section is not None and section.find("h3"):
            break
        section = section.find_parent("div") if section is not None else None
    if section is None:
        return []
    members, seen = [], set()
    for h3 in section.find_all("h3"):
        name = h3.get_text(" ", strip=True)
        if not name:
            continue
        p = h3.find_next_sibling("p")
        role = p.get_text(" ", strip=True) if p else ""
        key = (name, role)
        if key in seen:
            continue
        seen.add(key)
        members.append((name, role))
    return members


def fetch(url, tries=3):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return r.text, None
            last = f"HTTP {r.status_code}"
            if r.status_code in (403, 404):
                break
        except requests.RequestException as e:
            last = str(e)
        time.sleep(2 * (i + 1))
    return None, last


def main():
    wb = openpyxl.load_workbook(INPUT_FILE)
    ag = wb["Agences"]
    hdr = {c.value: c.column for c in ag[1]}
    c_id, c_nom, c_site = hdr["ID agence"], hdr["Nom agence"], hdr["Site internet"]
    c_siren = hdr.get("SIREN")

    if "Contacts" in wb.sheetnames:
        ct = wb["Contacts"]
        if ct.max_row > 1:
            ct.delete_rows(2, ct.max_row)
    else:
        ct = wb.create_sheet("Contacts")
        ct.append(CONTACT_HEADERS)

    errors, total = [], 0
    rows = [r for r in range(2, ag.max_row + 1) if ag.cell(r, c_id).value]
    if LIMIT:
        rows = rows[:LIMIT]

    for n, r in enumerate(rows, 1):
        aid = ag.cell(r, c_id).value
        nom_ag = ag.cell(r, c_nom).value
        url = ag.cell(r, c_site).value
        siren = ag.cell(r, c_siren).value if c_siren else None
        if not url:
            errors.append((aid, nom_ag, url, "pas de site"))
            continue
        html, err = fetch(url)
        if err:
            errors.append((aid, nom_ag, url, err))
            print(f"[{n}/{len(rows)}] {aid} ERREUR {err}", flush=True)
            time.sleep(DELAY)
            continue
        members = parse_team(html)
        if not members:
            errors.append((aid, nom_ag, url, "aucun membre trouvé"))
        for full, role in members:
            nom, prenom = split_name(full)
            ct.append([aid, siren, nom, prenom, role, None, None, None, url])
            total += 1
        print(f"[{n}/{len(rows)}] {aid} {nom_ag}: {len(members)} membres", flush=True)
        time.sleep(DELAY)

    ct.auto_filter.ref = f"A1:I{max(ct.max_row, 2)}"
    wb.save(INPUT_FILE)
    with open(ERRORS_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ID agence", "Nom agence", "URL", "Problème"])
        w.writerows(errors)
    print(f"Terminé : {total} membres, {len(errors)} agences à vérifier.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
