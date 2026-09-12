# Portail replays DataSuits

Espace privé où les apprenants regardent les replays des classes virtuelles,
sans possibilité de téléchargement et sans qu'aucune vidéo ne soit publique.

## Briques

| Brique | Rôle | Coût |
|---|---|---|
| monday | Source de vérité : apprenants + board « Replays CV » | existant |
| GitHub Actions | Robot nocturne `sync_portail.py` | gratuit (dépôt public) |
| Supabase | Comptes des apprenants, liste des replays, règles d'accès | gratuit |
| Cloudflare R2 | Coffre privé des vidéos | gratuit jusqu'à 10 Go |
| Cloudflare Worker | Vigile : ne sert une vidéo qu'à un apprenant connecté | gratuit |
| GitHub Pages | La page du portail (`docs/`) | gratuit |

## Au quotidien

1. Nouvelle CV enregistrée → dans OneDrive, **Partager** → « Toute personne
   disposant du lien », Affichage → copier le lien.
2. Dans monday, board **Replays CV** : nouvelle ligne, Module, Date CV, coller
   le lien. Statut « À traiter ».
3. La nuit suivante, le robot télécharge, compresse en 720p, range la vidéo
   dans le coffre et passe la ligne en « Publié ». Le Journal te dit quand
   retirer le lien de partage SharePoint.

Nouvel apprenant sur Slack → il apparaît avec un identifiant et un mot de passe
dans « Suivi des apprenants DA » (colonnes « Identifiant portail » /
« Mot de passe portail »), à lui transmettre dans sa room Slack.
Mot de passe perdu → cocher « Réinitialiser MDP ».
Abandon → accès coupé automatiquement.

## Lancer le robot à la main

Actions → **Portail replays** → Run workflow. Options : nombre de replays à
publier, et « à blanc » (lecture seule, aucune écriture) pour tester.

## Fichiers

- `sync_portail.py` — le robot
- `.github/workflows/portail.yml` — planification (chaque nuit 03:00 Paris)
- `supabase/schema.sql`, `supabase/patch_v2.sql` — base de données
- `docs/` — page du portail (GitHub Pages)
- `worker/` — vigile Cloudflare

Aucune donnée personnelle ni secret dans ce dépôt : tout est dans
Settings → Secrets, Supabase et monday.
