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
   Les vidéos restent sur SharePoint (lien « Toute personne », téléchargement
   bloqué par Microsoft). Le robot ne copie rien : il publie la LISTE.
   - Statut « À traiter » → vérification du lien, fiche Supabase, statut « Publié ».
   - Statut « Publié »    → la fiche est resynchronisée (titre, module, date…).
   - Statut « Archivé » (posé à la main) ou date de purge dépassée
     → la fiche est dépubliée, le replay disparaît du portail.

3. Le robot se lance à la main (bouton Run workflow). Pense à le lancer au
   moins une fois par semaine : chaque exécution « réveille » Supabase, dont
   le projet gratuit se met en pause après 7 jours sans activité.

Secrets attendus (GitHub → Settings → Secrets → Actions) :
  MONDAY_TOKEN, SUPABASE_URL, SUPABASE_SERVICE_KEY

Options (variables d'environnement, posées par le workflow) :
  DRY_RUN      "1" = tout lire, ne rien écrire (ni monday, ni Supabase)
"""

import datetime as dt
import json
import os
import re
import secrets
import sys
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
DRY_RUN = os.environ.get("DRY_RUN", "0").strip() == "1"

LOGIN_DOMAIN = "datasuits.fr"
CONSERVATION_MOIS = 12          # purge = date de la CV + 12 mois

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

def nettoyer_lien(lien):
    """Lien de partage SharePoint : on retire le traceur nav=… et on vérifie la forme."""
    lien = re.sub(r"[&?]nav=[^&]*", "", (lien or "").strip())
    if not re.match(r"https://[a-z0-9-]+-my\.sharepoint\.com/:[a-z]:/g/personal/", lien):
        raise RuntimeError(
            "le lien doit être un lien de PARTAGE SharePoint (bouton Partager → Copier le lien, "
            "forme https://…-my.sharepoint.com/:v:/g/personal/…), pas l'adresse de la barre du navigateur.")
    return lien


def date_purge(date_cv):
    if not date_cv:
        return None
    d = dt.date.fromisoformat(date_cv)
    mois = d.month - 1 + CONSERVATION_MOIS
    return d.replace(year=d.year + mois // 12, month=mois % 12 + 1, day=1)


def fiche_replay(it, publie, lien=None):
    v = it["v"]
    return {
        "monday_item_id": it["id"], "titre": it["name"],
        "formation": v[C_REP["formation"]] or FORMATION_APP,
        "module": v[C_REP["module"]] or "Autre",
        "promo_origine": v[C_REP["promo"]] or None,
        "date_cv": v[C_REP["date_cv"]] or None,
        "lien": lien, "publie": publie,
        "purge_le": (date_purge(v[C_REP["date_cv"]]) or dt.date(2099, 1, 1)).isoformat()
                    if publie else None,
        "maj_le": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


def sync_replays():
    log("\n══ 2. Replays ══")
    items = monday_items(B_REP, list(C_REP.values()))
    items.sort(key=lambda it: (it["v"][C_REP["date_cv"]] or "9999", it["name"]))
    deja = {r["monday_item_id"]: r for r in sb_select("replays", {"select": "monday_item_id,publie"})}

    publies = archives = erreurs = 0
    a_publier, a_archiver = [], []
    for it in items:
        st = it["v"][C_REP["statut"]]
        purge = it["v"][C_REP["purge"]]
        if st == "À traiter":
            try:
                lien = nettoyer_lien(it["v"][C_REP["lien"]])
            except RuntimeError as e:
                erreurs += 1
                log(f"   ❌ {it['name']} : {e}")
                monday_update(B_REP, it["id"], {
                    C_REP["statut"]: {"label": "Erreur"},
                    C_REP["journal"]: f"❌ {MAINTENANT} — {e}\nCorrige puis remets « À traiter »."})
                continue
            a_publier.append(fiche_replay(it, True, lien))
            p = date_purge(it["v"][C_REP["date_cv"]])
            monday_update(B_REP, it["id"], {
                C_REP["statut"]: {"label": "Publié"},
                C_REP["purge"]: {"date": p.isoformat()} if p else None,
                C_REP["journal"]: f"✅ Publié le {MAINTENANT}. Visible sur le portail (lecture seule, "
                                  f"téléchargement bloqué par SharePoint)."})
            publies += 1
            log(f"   ✅ {it['name']} → publié")
        elif st == "Publié":
            if purge and dt.date.fromisoformat(purge) <= AUJOURDHUI:
                a_archiver.append(it); continue
            try:
                a_publier.append(fiche_replay(it, True, nettoyer_lien(it["v"][C_REP["lien"]])))
            except RuntimeError as e:
                log(f"   ⚠️ {it['name']} : {e}")
        elif st == "Archivé" and deja.get(it["id"], {}).get("publie"):
            a_archiver.append(it)

    for it in a_archiver:
        motif = "date de purge atteinte" if it["v"][C_REP["statut"]] == "Publié" else "archivage manuel"
        sb_upsert("replays", [fiche_replay(it, False)], "monday_item_id")
        monday_update(B_REP, it["id"], {
            C_REP["statut"]: {"label": "Archivé"},
            C_REP["journal"]: f"📦 Archivé le {MAINTENANT} ({motif}) : retiré du portail. "
                              f"Tu peux supprimer le lien de partage SharePoint."})
        archives += 1
        log(f"   📦 {it['name']} archivé ({motif})")

    if a_publier:
        sb_upsert("replays", a_publier, "monday_item_id")
    log(f"   → {len(a_publier)} replays en ligne · {publies} nouveaux · {archives} archivés · {erreurs} en erreur")


# ─────────────────────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    for nom, val in [("MONDAY_TOKEN", MONDAY_TOKEN), ("SUPABASE_URL", SUPABASE_URL),
                     ("SUPABASE_SERVICE_KEY", SUPABASE_KEY)]:
        if not val:
            fatal(f"secret manquant : {nom}")
    log(f"🤖 Portail replays — {MAINTENANT}" + ("  [DRY-RUN : aucune écriture]" if DRY_RUN else ""))
    sync_apprenants()
    sync_replays()
    log("\n🏁 Terminé.")


if __name__ == "__main__":
    main()
