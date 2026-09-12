-- Correctif v2 — à coller dans SQL Editor après schema.sql
-- La colonne « email » devient l'identifiant de connexion (prenom.nom@datasuits.fr).
-- L'adresse personnelle est conservée à part, à titre d'information.

alter table public.apprenants add column if not exists email_perso text;
alter table public.apprenants add column if not exists slack       text;
alter table public.apprenants add column if not exists auth_uid    uuid;

comment on column public.apprenants.email       is 'Identifiant portail (prenom.nom@datasuits.fr), en minuscules';
comment on column public.apprenants.email_perso is 'Adresse personnelle (colonne monday « Email perso »), information seulement';
comment on column public.apprenants.slack       is 'Étape de bascule Slack au moment de la synchro';
comment on column public.apprenants.auth_uid    is 'Identifiant du compte Supabase Auth, posé par le robot';

-- Le portail n'utilise pas d'e-mail : on désactive la confirmation
-- et le lien magique n'est jamais envoyé. (Réglage aussi visible dans
-- Authentication → Providers → Email : « Confirm email » décoché.)
