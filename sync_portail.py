#!/usr/bin/env python3
"""
Portail replays DataSuits — robot de synchronisation
=====================================================

Ce que fait le robot, dans l'ordre :

1. APPRENANTS  (board monday « Suivi des apprenants DA »)
   - Tout apprenant dont la colonne « Slack » n'est pas vide reçoit un accès :
       identifiant  prenom.nom@datasuits.fr
       mot de passe Prenom.Promo.xxxxx   (xxxxx = 5 caractères aléatoires)
     Le compte est créé dans Supabase, l'identifiant et le mot de passe sont
     écrits dans les colonnes « Identifiant portail » / « Mot de passe portail ».
   - Case « Réinitialiser MDP » cochée → nouveau mot de passe, case décochée.
   - Statut « Abandon » → accès coupé (compte banni, actif = false).

2. REPLAYS  (board monday « Replays CV »)
   - Statut « À traiter » → téléchargement du lien SharePoint, compression 720p,
     dépôt dans le coffre R2, publication dans Supabase, statut « Publié ».
     Quelques replays par exécution (MAX_REPLAYS), choisis au lancement.
   - Statut « Archivé » (posé à la main) ou date de purge dépassée
     → dépublication + suppression du fichier dans R2.

3. Le robot se lance à la main (bouton Run workflow). Pense à le lancer au
   moins une fois par semaine : chaque exécution « réveille » Supabase, dont
   le projet gratuit se met en pause après 7 jours sans activité.

Secrets attendus (GitHub → Settings → Secrets → Actions) :
  MONDAY_TOKEN, SUPABASE_URL, SUPABASE_SERVICE_KEY,
  R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_ENDPOINT

Options (variables d'environnement, posées par le workflow) :
  MAX_REPLAYS  nombre de replays « À traiter » traités par exécution (défaut 3)
  DRY_RUN      "1" = tout lire, ne rien écrire (ni monday, ni Supabase, ni R2)
"""

import datetime as dt
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

# ─────────────────────────────────────────────────────────────────────────────
#  Configuration
# ─────────────────────────────────────────────────────────────────────────────

MONDAY_TOKEN = os.environ.get("MONDAY_TOKEN", "").strip()
SUPABASE_URL = re.sub(r"/(rest|auth|storage)/v1/?$", "",
                      os.environ.get("SUPABASE_URL", "").strip().rstrip("/")).rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
R2_SECRET_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
R2_ENDPOINT = os.environ.get("R2_ENDPOINT", "").strip()
R2_BUCKET = "replays-datasuits"

MAX_REPLAYS = int(os.environ.get("MAX_REPLAYS", "3") or 3)
DRY_RUN = os.environ.get("DRY_RUN", "0").strip() == "1"

LOGIN_DOMAIN = "datasuits.fr"
CONSERVATION_MOIS = 12          # purge = date de la CV + 12 mois
HAUTEUR_MAX = 720               # compression : 720p maximum

# Board « Suivi des apprenants DA »
B_APP = 1942427259
C_APP = {
    "email_perso": "text_mm35yrd2",
    "slack":       "color_mm6qf89m",
    "statut":      "color_mkxgrtgw",
    "formation":   "color_mkqncm8p",   # non utilisé pour l'instant (En cours…)
    "login":       "text_mm74tagg",
    "mdp":         "text_mm74z276",
    "reset":       "boolean_mm74ym3h",
}
FORMATION_APP = "Data Analyst"          # ce board = formation Data Analyst

# Board « Replays CV »
B_REP = 5104046423
C_REP = {
    "formation": "color_mm74ssrw",
    "module":    "color_mm749w77",
    "promo":     "text_mm74yp4q",
    "date_cv":   "date_mm74jbph",
    "lien":      "text_mm74v58z",
    "statut":    "color_mm74apxd",
    "duree":     "numeric_mm74d5y6",
    "taille":    "numeric_mm74g7y6",
    "purge":     "date_mm74ty7f",
    "journal":   "long_text_mm74rpcs",
}

AUJOURDHUI = dt.date.today()
MAINTENANT = dt.datetime.now().strftime("%d/%m/%Y %H:%M")


def log(msg):
    print(msg, flush=True)


def fatal(msg):
    log(f"❌ {msg}")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
#  HTTP minimal (urllib pur)
# ─────────────────────────────────────────────────────────────────────────────

def http(method, url, data=None, headers=None, timeout=120):
    body = None
    h = dict(headers or {})
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"HTTP {e.code} sur {method} {url} : {detail}") from None


# ─────────────────────────────────────────────────────────────────────────────
#  monday
# ─────────────────────────────────────────────────────────────────────────────

def monday(query, variables=None):
    r = http("POST", "https://api.monday.com/v2",
             {"query": query, "variables": variables or {}},
             {"Authorization": MONDAY_TOKEN})
    if r is None or r.get("errors"):
        raise RuntimeError(f"monday : {json.dumps(r, ensure_ascii=False)[:500]}")
    return r["data"]


def monday_items(board_id, column_ids):
    """Tous les éléments d'un board : id, nom, groupe, valeurs de colonnes."""
    items, cursor = [], None
    while True:
        q = """
        query($b:[ID!], $c:[String!], $cur:String) {
          boards(ids:$b) { items_page(limit:200, cursor:$cur) {
            cursor items { id name group { title }
              column_values(ids:$c) { id text value } } } } }"""
        d = monday(q, {"b": [str(board_id)], "c": column_ids, "cur": cursor})
        page = d["boards"][0]["items_page"]
        for it in page["items"]:
            vals = {cv["id"]: (cv["text"] or "").strip() for cv in it["column_values"]}
            raw = {cv["id"]: cv["value"] for cv in it["column_values"]}
            items.append({"id": it["id"], "name": it["name"].strip(),
                          "groupe": it["group"]["title"], "v": vals, "raw": raw})
        cursor = page["cursor"]
        if not cursor:
            return items


def monday_update(board_id, item_id, values):
    if DRY_RUN:
        log(f"   (dry-run) monday {item_id} ← {json.dumps(values, ensure_ascii=False)}")
        return
    q = """mutation($b:ID!, $i:ID!, $v:JSON!) {
             change_multiple_column_values(board_id:$b, item_id:$i, column_values:$v) { id } }"""
    monday(q, {"b": str(board_id), "i": str(item_id), "v": json.dumps(values)})


# ─────────────────────────────────────────────────────────────────────────────
#  Supabase (REST + Auth admin, clé service)
# ─────────────────────────────────────────────────────────────────────────────

SB_H = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}


def sb_select(table, params):
    url = f"{SUPABASE_URL}/rest/v1/{table}?{urllib.parse.urlencode(params)}"
    return http("GET", url, headers=SB_H) or []


def sb_upsert(table, rows, on_conflict):
    if DRY_RUN:
        log(f"   (dry-run) supabase upsert {table} × {len(rows)}")
        return
    url = f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}"
    http("POST", url, rows, {**SB_H, "Prefer": "resolution=merge-duplicates,return=minimal"})


def sb_auth_create(email, password, meta):
    if DRY_RUN:
        return "dry-run-uid"
    r = http("POST", f"{SUPABASE_URL}/auth/v1/admin/users",
             {"email": email, "password": password, "email_confirm": True,
              "user_metadata": meta}, SB_H)
    return r["id"]


def sb_auth_update(uid, payload):
    if DRY_RUN:
        return
    http("PUT", f"{SUPABASE_URL}/auth/v1/admin/users/{uid}", payload, SB_H)


# ─────────────────────────────────────────────────────────────────────────────
#  Identifiants et mots de passe
# ─────────────────────────────────────────────────────────────────────────────

def ascii_fold(s):
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def fabriquer_login(nom_complet, deja_pris):
    mots = [m for m in re.split(r"\s+", nom_complet.strip()) if m]
    if not mots:
        return None
    prenom = ascii_fold(mots[0])
    nom = ascii_fold(mots[-1]) if len(mots) > 1 else "x"
    base = f"{prenom}.{nom}"
    login, n = f"{base}@{LOGIN_DOMAIN}", 2
    while login in deja_pris:
        login, n = f"{base}{n}@{LOGIN_DOMAIN}", n + 1
    deja_pris.add(login)
    return login


ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # sans 0/O, 1/l/i : lisible sur Slack


def fabriquer_mdp(nom_complet, promo):
    prenom = re.split(r"\s+", nom_complet.strip())[0]
    prenom = "".join(c for c in unicodedata.normalize("NFKD", prenom)
                     if not unicodedata.combining(c))
    prenom = re.sub(r"[^A-Za-z]", "", prenom).capitalize() or "Ds"
    alea = "".join(secrets.choice(ALPHABET) for _ in range(5))
    return f"{prenom}.{promo or 'DS'}.{alea}"


# ─────────────────────────────────────────────────────────────────────────────
#  1) APPRENANTS
# ─────────────────────────────────────────────────────────────────────────────

def sync_apprenants():
    log("\n══ 1. Apprenants ══")
    items = monday_items(B_APP, list(C_APP.values()))
    existants = {r["monday_item_id"]: r
                 for r in sb_select("apprenants", {"select": "monday_item_id,email,auth_uid,actif"})}
    logins_pris = {r["email"] for r in existants.values()}
    logins_pris |= {it["v"][C_APP["login"]].lower() for it in items if it["v"][C_APP["login"]]}

    crees = resets = coupes = 0
    rows = []
    for it in items:
        v = it["v"]
        eligible = bool(v[C_APP["slack"]])
        if not eligible:
            continue
        actif = v[C_APP["statut"]] != "Abandon"
        deja = existants.get(it["id"])
        login = v[C_APP["login"]].lower() or (deja and deja["email"])
        reset = v[C_APP["reset"]] == "v" or v[C_APP["reset"]].lower() in ("true", "checked", "✓")

        try:
            if not login:
                # ── nouveau compte (seulement si l'apprenant est actif) ──
                if not actif:
                    continue
                login = fabriquer_login(it["name"], logins_pris)
                mdp = fabriquer_mdp(it["name"], it["groupe"])
                rows_now = [{"monday_item_id": it["id"], "email": login, "nom": it["name"],
                             "formation": FORMATION_APP, "promo": it["groupe"],
                             "email_perso": v[C_APP["email_perso"]].lower() or None,
                             "slack": v[C_APP["slack"]], "actif": actif,
                             "maj_le": dt.datetime.now(dt.timezone.utc).isoformat()}]
                sb_upsert("apprenants", rows_now, "monday_item_id")   # avant auth (garde-fou)
                uid = sb_auth_create(login, mdp, {"nom": it["name"], "promo": it["groupe"],
                                                   "formation": FORMATION_APP})
                sb_upsert("apprenants", [{"monday_item_id": it["id"], "auth_uid": uid,
                                          "email": login, "nom": it["name"],
                                          "formation": FORMATION_APP}], "monday_item_id")
                monday_update(B_APP, it["id"], {C_APP["login"]: login, C_APP["mdp"]: mdp})
                log(f"   ✨ {it['name']} ({it['groupe']}) → {login}")
                crees += 1
                continue

            # ── compte existant : mise à jour de la fiche ──
            rows.append({"monday_item_id": it["id"], "email": login, "nom": it["name"],
                         "formation": FORMATION_APP, "promo": it["groupe"],
                         "email_perso": v[C_APP["email_perso"]].lower() or None,
                         "slack": v[C_APP["slack"]], "actif": actif,
                         "maj_le": dt.datetime.now(dt.timezone.utc).isoformat()})
            uid = deja and deja.get("auth_uid")

            if reset and uid:
                mdp = fabriquer_mdp(it["name"], it["groupe"])
                sb_auth_update(uid, {"password": mdp})
                monday_update(B_APP, it["id"], {C_APP["mdp"]: mdp, C_APP["reset"]: {"checked": "false"}})
                log(f"   🔑 {it['name']} : nouveau mot de passe")
                resets += 1

            if uid and deja is not None and deja["actif"] != actif:
                sb_auth_update(uid, {"ban_duration": "none" if actif else "876600h"})
                log(f"   {'🔓' if actif else '⛔'} {it['name']} : accès {'rétabli' if actif else 'coupé'}")
                if not actif:
                    coupes += 1
        except Exception as e:
            log(f"   ⚠️ {it['name']} : {e}")

    if rows:
        sb_upsert("apprenants", rows, "monday_item_id")
    log(f"   → {len(rows) + crees} apprenants synchronisés · {crees} comptes créés · "
        f"{resets} mots de passe régénérés · {coupes} accès coupés")


# ─────────────────────────────────────────────────────────────────────────────
#  2) REPLAYS
# ─────────────────────────────────────────────────────────────────────────────

def r2_client():
    import boto3  # installé par le workflow
    return boto3.client("s3", endpoint_url=R2_ENDPOINT,
                        aws_access_key_id=R2_ACCESS_KEY,
                        aws_secret_access_key=R2_SECRET_KEY, region_name="auto")


def urls_telechargement(lien):
    """Lien de partage SharePoint → liste d'URL de téléchargement à essayer."""
    lien = re.sub(r"[&?]nav=[^&]*", "", lien.strip())
    urls = [lien + ("&" if "?" in lien else "?") + "download=1"]
    # Forme download.aspx?share=<jeton> : la plus fiable pour les liens « Toute personne »
    m = re.match(r"(https://[^/]+/)(?::[a-z]:/g/)?(personal/[^/]+)/([A-Za-z0-9_-]{20,})", lien)
    if m:
        racine, perso, jeton = m.groups()
        urls.append(f"{racine}{perso}/_layouts/15/download.aspx?share={jeton}")
    return urls


def telecharger(lien, dest):
    """Télécharge la vidéo en essayant chaque forme d'URL, avec gestion des cookies."""
    import http.cookiejar
    ouvreur = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    ouvreur.addheaders = [("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"),
                          ("Accept", "*/*")]
    erreurs = []
    for url in urls_telechargement(lien):
        try:
            with ouvreur.open(url, timeout=60) as r:
                ctype = r.headers.get("Content-Type", "")
                if "text/html" in ctype:
                    page = r.read(20000).decode("utf-8", "replace")
                    titre = re.search(r"<title>(.*?)</title>", page, re.S | re.I)
                    titre = (titre.group(1).strip()[:80] if titre else "page sans titre")
                    erreurs.append(f"{r.geturl()[:90]}… → page web « {titre} »")
                    continue
                with open(dest, "wb") as f:
                    shutil.copyfileobj(r, f, length=1024 * 1024)
            return os.path.getsize(dest)
        except urllib.error.HTTPError as e:
            erreurs.append(f"HTTP {e.code} sur {url[:90]}…")
        except Exception as e:
            erreurs.append(f"{type(e).__name__} : {e}")
    raise RuntimeError(
        "Téléchargement impossible. Vérifie sur OneDrive que le lien est bien "
        "« Toute personne disposant du lien » ET que « Bloquer le téléchargement » "
        "est désactivé (la fenêtre de partage Stream l'active parfois par défaut). "
        "Détail : " + " | ".join(erreurs))


def ffprobe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=height:format=duration",
                          "-of", "json", path], capture_output=True, text=True, check=True).stdout
    j = json.loads(out)
    duree = float(j.get("format", {}).get("duration") or 0)
    hauteur = int((j.get("streams") or [{}])[0].get("height") or 0)
    return duree, hauteur


def compresser(src, dst):
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", src,
                    "-vf", f"scale=-2:'min({HAUTEUR_MAX},ih)'",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "27",
                    "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", dst], check=True)


def publier_replay(it, s3):
    v = it["v"]
    titre = it["name"]
    log(f"\n   ▶ {titre}")
    if not v[C_REP["lien"]]:
        raise RuntimeError("colonne « Lien enregistrement » vide")
    monday_update(B_REP, it["id"], {C_REP["statut"]: {"label": "En cours"}})

    with tempfile.TemporaryDirectory() as tmp:
        brut, fin = os.path.join(tmp, "brut.mp4"), os.path.join(tmp, "final.mp4")
        taille_brute = telecharger(v[C_REP["lien"]], brut)
        duree, hauteur = ffprobe(brut)
        log(f"     téléchargé {taille_brute/1e6:.0f} Mo · {duree/60:.0f} min · {hauteur}p")

        if hauteur > HAUTEUR_MAX or taille_brute > duree / 3600 * 450e6:
            log("     compression 720p…")
            compresser(brut, fin)
        else:
            os.rename(brut, fin)
        taille = os.path.getsize(fin)
        log(f"     prêt : {taille/1e6:.0f} Mo")

        key = f"data-analyst/{it['id']}.mp4"
        if DRY_RUN:
            log(f"     (dry-run) upload R2 → {key}")
        else:
            s3.upload_file(fin, R2_BUCKET, key, ExtraArgs={"ContentType": "video/mp4"})
            log(f"     déposé dans R2 → {key}")

    date_cv = v[C_REP["date_cv"]] or None
    purge = None
    if date_cv:
        d = dt.date.fromisoformat(date_cv)
        mois = d.month - 1 + CONSERVATION_MOIS
        purge = d.replace(year=d.year + mois // 12, month=mois % 12 + 1, day=1)

    sb_upsert("replays", [{
        "monday_item_id": it["id"], "titre": titre,
        "formation": v[C_REP["formation"]] or FORMATION_APP,
        "module": v[C_REP["module"]] or "Autre",
        "promo_origine": v[C_REP["promo"]] or None, "date_cv": date_cv,
        "duree_min": round(duree / 60), "taille_mo": round(taille / 1e6),
        "r2_key": key, "publie": True,
        "purge_le": purge.isoformat() if purge else None,
        "maj_le": dt.datetime.now(dt.timezone.utc).isoformat(),
    }], "monday_item_id")

    monday_update(B_REP, it["id"], {
        C_REP["statut"]: {"label": "Publié"},
        C_REP["duree"]: str(round(duree / 60)),
        C_REP["taille"]: str(round(taille / 1e6)),
        C_REP["purge"]: {"date": purge.isoformat()} if purge else None,
        C_REP["journal"]: f"✅ Publié le {MAINTENANT} · {round(duree/60)} min · "
                          f"{round(taille/1e6)} Mo. Tu peux maintenant retirer le lien "
                          f"de partage SharePoint (le fichier est dans le coffre).",
    })
    log("     ✅ publié")


def archiver_replay(it, s3, motif):
    key = f"data-analyst/{it['id']}.mp4"
    if not DRY_RUN:
        try:
            s3.delete_object(Bucket=R2_BUCKET, Key=key)
        except Exception as e:
            log(f"     (R2) {e}")
    sb_upsert("replays", [{"monday_item_id": it["id"], "titre": it["name"],
                           "formation": it["v"][C_REP["formation"]] or FORMATION_APP,
                           "module": it["v"][C_REP["module"]] or "Autre",
                           "publie": False, "r2_key": None,
                           "maj_le": dt.datetime.now(dt.timezone.utc).isoformat()}], "monday_item_id")
    monday_update(B_REP, it["id"], {
        C_REP["statut"]: {"label": "Archivé"},
        C_REP["journal"]: f"📦 Archivé le {MAINTENANT} ({motif}) : retiré du portail, "
                          f"fichier supprimé du coffre.",
    })
    log(f"   📦 {it['name']} archivé ({motif})")


def sync_replays():
    log("\n══ 2. Replays ══")
    items = monday_items(B_REP, list(C_REP.values()))
    publies = {r["monday_item_id"]: r for r in sb_select("replays", {"select": "monday_item_id,publie"})}
    s3 = r2_client()

    a_traiter = [it for it in items if it["v"][C_REP["statut"]] == "À traiter"]
    a_traiter.sort(key=lambda it: (it["v"][C_REP["date_cv"]] or "9999", it["name"]))  # plus ancien d'abord
    log(f"   {len(a_traiter)} replay(s) à traiter, {MAX_REPLAYS} max pour cette exécution")
    for it in a_traiter[:MAX_REPLAYS]:
        try:
            publier_replay(it, s3)
        except Exception as e:
            log(f"     ❌ {e}")
            monday_update(B_REP, it["id"], {
                C_REP["statut"]: {"label": "Erreur"},
                C_REP["journal"]: f"❌ {MAINTENANT} — {str(e)[:900]}\n"
                                  f"Corrige puis remets le statut « À traiter ».",
            })

    for it in items:
        st = it["v"][C_REP["statut"]]
        purge = it["v"][C_REP["purge"]]
        deja_publie = publies.get(it["id"], {}).get("publie")
        if st == "Archivé" and deja_publie:
            archiver_replay(it, s3, "archivage manuel")
        elif st == "Publié" and purge and dt.date.fromisoformat(purge) <= AUJOURDHUI:
            archiver_replay(it, s3, "date de purge atteinte")


# ─────────────────────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    for nom, val in [("MONDAY_TOKEN", MONDAY_TOKEN), ("SUPABASE_URL", SUPABASE_URL),
                     ("SUPABASE_SERVICE_KEY", SUPABASE_KEY), ("R2_ACCESS_KEY_ID", R2_ACCESS_KEY),
                     ("R2_SECRET_ACCESS_KEY", R2_SECRET_KEY), ("R2_ENDPOINT", R2_ENDPOINT)]:
        if not val:
            fatal(f"secret manquant : {nom}")
    log(f"🤖 Portail replays — {MAINTENANT}" + ("  [DRY-RUN : aucune écriture]" if DRY_RUN else ""))
    sync_apprenants()
    sync_replays()
    log("\n🏁 Terminé.")


if __name__ == "__main__":
    main()
