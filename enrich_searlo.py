#!/usr/bin/env python3
"""
Enrichissement d'agences via Searlo  (GET /search/places).

- Lit un CSV (colonnes Nom;Adresse), dédoublonne (nom + adresse).
- Une requête par agence : q = Nom, location = Adresse, limit=3, page=1, hl=fr, gl=fr.
- Utilise les clés une à la fois (rotation quand une clé est épuisée / invalide).
- Écrit tout dans data/ :
    data/resultats_000.jsonl, resultats_001.jsonl, ...  (10 000 agences max par fichier)
    data/erreurs.jsonl                                  (échecs, pour diagnostic)
    data/state.json                                     (état : clés épuisées, stats, historique)
    data/logs/run_AAAAMMJJ_HHMMSS.log                   (un log par exécution)
- Reprise automatique : les id déjà présents dans resultats_*.jsonl sont ignorés.
- Arrêt propre (SIGINT/SIGTERM ou durée max) avec sauvegarde de l'état.
- Les clés ne sont JAMAIS écrites (seulement une empreinte de 8 caractères).

Secret attendu : SEARLO_API_KEYS  (une clé par ligne, ou séparées par virgule/espace)
"""
import argparse
import csv
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
BASE_URL = os.getenv("SEARLO_BASE_URL", "https://api.searlo.tech/api/v1").rstrip("/")
DATA_DIR = Path("data")
LOG_DIR = DATA_DIR / "logs"
STATE_FILE = DATA_DIR / "state.json"
ERRORS_FILE = DATA_DIR / "erreurs.jsonl"
CHUNK_SIZE = 10_000  # agences par fichier resultats_XXX.jsonl (reste sous la limite de 100 Mo de GitHub)

# Paramètres de recherche demandés
LIMIT, PAGE, HL, GL = 3, 1, "fr", "fr"

# Réglages (surchargeables par variables d'environnement)
MIN_INTERVAL = float(os.getenv("REQUEST_INTERVAL", "1.3"))  # quota places : 48 req/min => >= 1,25 s
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "4"))             # réessais par agence (5xx / réseau)
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "3"))           # nb d'échecs max par agence, tous runs confondus
MAX_CONSEC_ERRORS = int(os.getenv("MAX_CONSEC_ERRORS", "25"))  # disjoncteur
CHECKPOINT_MIN = float(os.getenv("CHECKPOINT_MIN", "45"))    # commit git intermédiaire (si GIT_CHECKPOINT=1)
STATE_EVERY = 20                                             # sauvegarde de state.json tous les N traitements

log = logging.getLogger("searlo")


# --------------------------------------------------------------------------- #
# Utilitaires
# --------------------------------------------------------------------------- #
def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:8]


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().casefold()


def parse_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def retry_after(resp, default=10.0, cap=120.0):
    v = parse_float(resp.headers.get("Retry-After"))
    return min(v if v and v > 0 else default, cap)


def find_balance(obj):
    """Cherche un solde de crédits dans une réponse JSON de forme inconnue."""
    names = {"credits", "balance", "remaining", "creditsremaining", "credits_remaining",
             "remainingcredits", "available"}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in names and isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v)
        for v in obj.values():
            r = find_balance(v)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_balance(v)
            if r is not None:
                return r
    return None


def setup_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"run_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.log"
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    log.setLevel(logging.DEBUG)
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)
    return path


def append_jsonl(path: Path, obj: dict):
    """Ajoute une ligne JSON. Répare un fichier dont la dernière ligne est tronquée (crash)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_nl = False
    if path.exists() and path.stat().st_size > 0:
        with open(path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            needs_nl = f.read(1) != b"\n"
    with open(path, "a", encoding="utf-8") as f:
        if needs_nl:
            f.write("\n")
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def iter_jsonl(path: Path):
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                log.warning("Ligne JSON illisible ignorée : %s:%d", path, n)


def result_file(row_id: int) -> Path:
    return DATA_DIR / f"resultats_{(row_id - 1) // CHUNK_SIZE:03d}.jsonl"


# --------------------------------------------------------------------------- #
# Entrée / reprise / état
# --------------------------------------------------------------------------- #
def load_rows(path: Path):
    """Retourne (lignes dédoublonnées, nb de doublons). id = n° de ligne dans le CSV (1 = 1re agence)."""
    rows, seen, dupes = [], set(), 0
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        for i, r in enumerate(reader, 1):
            nom = (r.get("Nom") or "").strip()
            adresse = (r.get("Adresse") or "").strip()
            if not nom:
                continue
            k = (norm(nom), norm(adresse))
            if k in seen:
                dupes += 1
                continue
            seen.add(k)
            rows.append({"id": i, "nom": nom, "adresse": adresse})
    return rows, dupes


def load_done():
    done = set()
    for p in sorted(DATA_DIR.glob("resultats_*.jsonl")):
        for rec in iter_jsonl(p):
            if "id" in rec:
                done.add(rec["id"])
    return done


def load_error_counts():
    counts = {}
    if ERRORS_FILE.exists():
        for rec in iter_jsonl(ERRORS_FILE):
            if rec.get("final"):
                counts[rec["id"]] = counts.get(rec["id"], 0) + 1
    return counts


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("state.json illisible : on repart d'un état vide")
    return {"version": 1, "exhausted": {}, "keys_usage": {}, "runs": []}


def save_state(state):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


# --------------------------------------------------------------------------- #
# Gestion des clés
# --------------------------------------------------------------------------- #
class KeyPool:
    def __init__(self, keys, state):
        self.keys = [(fingerprint(k), k) for k in keys]
        self.state = state
        self.exhausted = dict(state.get("exhausted", {}))  # fp -> raison
        self.cooldown = {}                                  # fp -> timestamp (monotonic)
        self.idx = 0

    def preflight(self, session):
        """Interroge /credit/balance (gratuit) pour chaque clé."""
        for fp, key in self.keys:
            try:
                r = session.get(f"{BASE_URL}/credit/balance", headers={"x-api-key": key}, timeout=(10, 30))
                if r.status_code == 200:
                    bal = find_balance(r.json())
                    log.info("Clé %s : solde=%s", fp, "inconnu" if bal is None else int(bal))
                    log.debug("Clé %s balance brute : %s", fp, r.text[:300])
                    if bal is not None:
                        if bal <= 0:
                            self.exhausted[fp] = "solde nul (preflight)"
                        else:
                            self.exhausted.pop(fp, None)  # recréditée => réactivée
                elif r.status_code in (401, 403):
                    self.exhausted[fp] = f"clé refusée HTTP {r.status_code}"
                    log.warning("Clé %s refusée (HTTP %s)", fp, r.status_code)
                else:
                    log.warning("Clé %s : balance HTTP %s %s", fp, r.status_code, r.text[:200])
            except (requests.RequestException, ValueError) as e:
                log.warning("Clé %s : balance impossible (%s)", fp, e)
        self.persist()

    def persist(self):
        self.state["exhausted"] = self.exhausted

    def current(self):
        n = len(self.keys)
        now = time.monotonic()
        for off in range(n):
            i = (self.idx + off) % n
            fp, key = self.keys[i]
            if fp in self.exhausted or self.cooldown.get(fp, 0) > now:
                continue
            if i != self.idx:
                log.info("Changement de clé -> %s (%d/%d)", fp, i + 1, n)
            self.idx = i
            return fp, key
        return None

    def mark_exhausted(self, fp, reason):
        self.exhausted[fp] = reason
        log.warning("Clé %s retirée : %s (%d/%d clés encore actives)",
                    fp, reason, len(self.keys) - len(self.exhausted), len(self.keys))
        self.persist()

    def set_cooldown(self, fp, seconds):
        self.cooldown[fp] = time.monotonic() + seconds
        log.warning("Clé %s en pause %d min (429 répétés)", fp, seconds // 60)

    def all_exhausted(self):
        return all(fp in self.exhausted for fp, _ in self.keys)

    def next_cooldown_wait(self):
        now = time.monotonic()
        waits = [self.cooldown[fp] - now for fp, _ in self.keys
                 if fp not in self.exhausted and self.cooldown.get(fp, 0) > now]
        return min(waits) if waits else 0.0


# --------------------------------------------------------------------------- #
# Traitement
# --------------------------------------------------------------------------- #
class Runner:
    def __init__(self, pool, state, deadline, max_requests):
        self.pool = pool
        self.state = state
        self.deadline = deadline
        self.max_requests = max_requests
        self.session = requests.Session()
        self.stop = False
        self.last_call = 0.0
        self.requests_made = 0
        self.ok = self.empty = self.errors = 0
        self.consec_errors = 0

    # -- contrôle ---------------------------------------------------------- #
    def should_stop(self):
        if self.stop:
            return True
        if time.monotonic() >= self.deadline:
            log.info("Durée maximale atteinte : arrêt propre")
            self.stop = True
        elif self.max_requests and self.requests_made >= self.max_requests:
            log.info("Nombre maximal de requêtes atteint (%d) : arrêt", self.max_requests)
            self.stop = True
        return self.stop

    def throttle(self):
        wait = MIN_INTERVAL - (time.monotonic() - self.last_call)
        if wait > 0:
            time.sleep(wait)
        self.last_call = time.monotonic()

    def usage(self, fp):
        u = self.state["keys_usage"].setdefault(fp, {"requetes": 0, "dernier_solde": None})
        return u

    # -- une agence -------------------------------------------------------- #
    def process(self, row):
        """Retourne 'done', 'error', 'stop' ou 'no_keys'."""
        q = row["nom"][:500]
        loc = row["adresse"][:200]
        params = {"q": q, "location": loc, "limit": LIMIT, "page": PAGE, "hl": HL, "gl": GL}
        retries = 0
        consec_429 = 0
        last_err = {"http_status": None, "code": None, "message": None}

        while True:
            if self.should_stop():
                return "stop"
            kp = self.pool.current()
            if kp is None:
                if self.pool.all_exhausted():
                    return "no_keys"
                wait = self.pool.next_cooldown_wait()
                if wait > self.deadline - time.monotonic():
                    self.stop = True
                    return "stop"
                time.sleep(min(wait, 60))
                continue
            fp, key = kp

            self.throttle()
            self.requests_made += 1
            try:
                resp = self.session.get(f"{BASE_URL}/search/places", params=params,
                                        headers={"x-api-key": key}, timeout=(10, 60))
            except requests.RequestException as e:
                retries += 1
                last_err = {"http_status": None, "code": "NETWORK", "message": str(e)[:300]}
                log.warning("id=%s réseau (%s) essai %d/%d", row["id"], e.__class__.__name__, retries, MAX_RETRIES)
                if retries > MAX_RETRIES:
                    return self.fail(row, fp, last_err)
                time.sleep(min(2 ** retries, 30))
                continue

            sc = resp.status_code
            remaining = parse_float(resp.headers.get("X-Credits-Remaining"))
            u = self.usage(fp)
            u["requetes"] += 1
            if remaining is not None:
                u["dernier_solde"] = remaining

            if sc == 200:
                try:
                    data = resp.json()
                except ValueError:
                    retries += 1
                    last_err = {"http_status": 200, "code": "BAD_JSON", "message": resp.text[:300]}
                    if retries > MAX_RETRIES:
                        return self.fail(row, fp, last_err)
                    time.sleep(2)
                    continue
                places = data.get("places") or []
                status = "ok" if places else "empty"
                append_jsonl(result_file(row["id"]), {
                    "id": row["id"], "nom": row["nom"], "adresse": row["adresse"],
                    "query": q, "location": loc, "status": status,
                    "nb_resultats": len(places), "places": places, "lite": data.get("lite"),
                    "key": fp, "credits_restants": remaining, "fetched_at": now_iso(),
                })
                if status == "ok":
                    self.ok += 1
                else:
                    self.empty += 1
                self.consec_errors = 0
                log.info("%s id=%s key=%s résultats=%d restant=%s",
                         status.upper(), row["id"], fp, len(places), remaining)
                if remaining is not None and remaining <= 0:
                    self.pool.mark_exhausted(fp, "crédits à 0")
                return "done"

            body = resp.text[:300].replace("\n", " ")
            try:
                code = resp.json().get("code")
            except ValueError:
                code = None
            last_err = {"http_status": sc, "code": code, "message": body}

            if sc == 402:                       # crédits épuisés : on change de clé, sans compter d'échec
                self.pool.mark_exhausted(fp, "402 crédits insuffisants")
                continue
            if sc in (401, 403):                # clé invalide / compte désactivé
                self.pool.mark_exhausted(fp, f"clé refusée HTTP {sc}")
                continue
            if sc == 429:
                consec_429 += 1
                wait = retry_after(resp)
                log.warning("id=%s key=%s 429 (%s) pause %.0fs [%d]", row["id"], fp, code, wait, consec_429)
                if consec_429 >= 5:
                    self.pool.set_cooldown(fp, 3600)
                    consec_429 = 0
                else:
                    time.sleep(wait)
                continue
            if sc >= 500:                       # 5xx : réessayable (non facturé côté Searlo)
                retries += 1
                log.warning("id=%s key=%s HTTP %s (%s) essai %d/%d", row["id"], fp, sc, code, retries, MAX_RETRIES)
                if retries > MAX_RETRIES:
                    return self.fail(row, fp, last_err)
                time.sleep(min(retry_after(resp, default=2 ** retries, cap=60), 60))
                continue
            # 400 / 404 / 422... : erreur définitive pour cette agence
            return self.fail(row, fp, last_err)

    def fail(self, row, fp, err):
        self.errors += 1
        self.consec_errors += 1
        append_jsonl(ERRORS_FILE, {
            "id": row["id"], "nom": row["nom"], "adresse": row["adresse"], "final": True,
            "key": fp, "at": now_iso(), **err,
        })
        log.error("ERREUR id=%s nom=%r http=%s code=%s msg=%s",
                  row["id"], row["nom"], err["http_status"], err["code"], err["message"])
        return "error"


# --------------------------------------------------------------------------- #
# Checkpoint git (commit intermédiaire pour ne rien perdre si le job est tué)
# --------------------------------------------------------------------------- #
def git_checkpoint(message):
    try:
        subprocess.run(["git", "add", "data"], check=False)
        if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
            return
        subprocess.run(["git", "commit", "-m", message], check=False, stdout=subprocess.DEVNULL)
        for _ in range(3):
            if subprocess.run(["git", "push"], check=False, stdout=subprocess.DEVNULL).returncode == 0:
                log.info("Checkpoint git poussé")
                return
            subprocess.run(["git", "pull", "--rebase", "--autostash"], check=False, stdout=subprocess.DEVNULL)
        log.warning("Checkpoint git : push impossible (sera repris à la fin du job)")
    except Exception as e:  # ne doit jamais casser l'enrichissement
        log.warning("Checkpoint git échoué : %s", e)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Enrichissement des agences via Searlo /search/places")
    ap.add_argument("--input", default=os.getenv("INPUT_FILE", "data/agences_nettoyees.csv"))
    ap.add_argument("--max-runtime-min", type=float, default=float(os.getenv("MAX_RUNTIME_MIN", "335")))
    ap.add_argument("--max-requests", type=int, default=int(os.getenv("MAX_REQUESTS", "0") or 0),
                    help="0 = illimité (utile pour tester : --max-requests 20)")
    ap.add_argument("--retry-errors", action="store_true",
                    default=os.getenv("RETRY_ERRORS", "").lower() in ("1", "true", "yes"),
                    help="retente aussi les agences ayant déjà atteint MAX_ATTEMPTS échecs")
    args = ap.parse_args()

    log_path = setup_logging()
    log.info("=== Démarrage (log : %s) ===", log_path)

    keys = list(dict.fromkeys(k for k in re.split(r"[\s,;]+", os.getenv("SEARLO_API_KEYS", "")) if k))
    if not keys:
        log.error("SEARLO_API_KEYS vide ou absent")
        return 2
    in_path = Path(args.input)
    if not in_path.exists():
        log.error("Fichier d'entrée introuvable : %s", in_path)
        return 2

    rows, dupes = load_rows(in_path)
    done = load_done()
    err_counts = {} if args.retry_errors else load_error_counts()
    todo = [r for r in rows if r["id"] not in done and err_counts.get(r["id"], 0) < MAX_ATTEMPTS]
    skipped_err = sum(1 for r in rows if r["id"] not in done and err_counts.get(r["id"], 0) >= MAX_ATTEMPTS)
    log.info("Agences : %d (doublons ignorés : %d) | déjà faites : %d | abandonnées (>=%d échecs) : %d | à faire : %d",
             len(rows), dupes, len(done), MAX_ATTEMPTS, skipped_err, len(todo))
    log.info("%d clé(s) chargée(s)", len(keys))

    state = load_state()
    started = now_iso()
    if not todo:
        log.info("Rien à faire : tout est déjà traité.")
        state["runs"] = (state.get("runs", []) + [{"start": started, "end": now_iso(), "note": "rien à faire"}])[-20:]
        save_state(state)
        return 0

    pool = KeyPool(keys, state)
    runner = Runner(pool, state, time.monotonic() + args.max_runtime_min * 60, args.max_requests)
    runner.session.headers["Accept"] = "application/json"
    pool.preflight(runner.session)
    save_state(state)
    if pool.all_exhausted():
        log.error("Aucune clé avec des crédits. Fin.")
        return 0

    def on_signal(signum, _frame):
        log.warning("Signal %s reçu : arrêt propre après la requête en cours", signum)
        runner.stop = True

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    use_git = os.getenv("GIT_CHECKPOINT", "") == "1"
    next_ckpt = time.monotonic() + CHECKPOINT_MIN * 60
    outcome, processed, exit_code = "terminé", 0, 0

    for row in todo:
        res = runner.process(row)
        if res == "stop":
            outcome = "arrêt demandé / durée max / limite de requêtes"
            break
        if res == "no_keys":
            outcome = "toutes les clés sont épuisées"
            break
        processed += 1
        if processed % STATE_EVERY == 0:
            save_state(state)
        if processed % 100 == 0:
            log.info("--- Progression run : %d traitées | ok=%d vides=%d erreurs=%d | restantes=%d",
                     processed, runner.ok, runner.empty, runner.errors, len(todo) - processed)
        if runner.consec_errors >= MAX_CONSEC_ERRORS:
            outcome = f"disjoncteur : {MAX_CONSEC_ERRORS} erreurs consécutives"
            log.error(outcome)
            exit_code = 1
            break
        if use_git and time.monotonic() >= next_ckpt:
            save_state(state)
            git_checkpoint(f"data: checkpoint {now_iso()} ({processed} agences ce run)")
            next_ckpt = time.monotonic() + CHECKPOINT_MIN * 60

    summary = {"start": started, "end": now_iso(), "outcome": outcome, "traitees": processed,
               "ok": runner.ok, "vides": runner.empty, "erreurs": runner.errors,
               "requetes": runner.requests_made, "restantes": len(todo) - processed,
               "cles_actives": len(pool.keys) - len(pool.exhausted)}
    state["runs"] = (state.get("runs", []) + [summary])[-20:]
    state["last_summary"] = summary
    save_state(state)
    log.info("=== Fin : %s ===", json.dumps(summary, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
