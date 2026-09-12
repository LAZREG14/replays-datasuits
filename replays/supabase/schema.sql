-- ============================================================
--  Portail Replays CV — schéma Supabase (à coller dans SQL Editor)
--  Version 1 — 3 tables, 1 garde-fou, règles d'accès (RLS)
-- ============================================================

-- 1) Apprenants autorisés — alimentée par GitHub Actions depuis monday
create table if not exists public.apprenants (
  id              uuid primary key default gen_random_uuid(),
  email           text not null unique,          -- Email perso, en minuscules
  nom             text not null,                 -- Nom affiché (item monday)
  formation       text not null,                 -- 'Data Analyst', 'Product Builder', 'SKILLS'
  promo           text,                          -- Groupe monday : P0426, P0525…
  actif           boolean not null default true, -- false = accès coupé sans supprimer
  monday_item_id  text unique,
  maj_le          timestamptz not null default now()
);

-- 2) Replays — alimentée par GitHub Actions depuis le board « Replays CV »
create table if not exists public.replays (
  id              uuid primary key default gen_random_uuid(),
  monday_item_id  text not null unique,
  titre           text not null,
  formation       text not null,
  module          text not null,                 -- 'Module 1' … 'Projet Final'
  promo_origine   text,
  date_cv         date,
  duree_min       integer,
  taille_mo       integer,
  r2_key          text,                          -- chemin du fichier dans le coffre R2
  publie          boolean not null default false,
  purge_le        date,
  maj_le          timestamptz not null default now()
);

-- 3) Lectures — trace « qui a ouvert quel replay » (utile Qualiopi)
create table if not exists public.lectures (
  id              bigint generated always as identity primary key,
  email           text not null,
  replay_id       uuid not null references public.replays(id) on delete cascade,
  ouvert_le       timestamptz not null default now()
);
create index if not exists lectures_replay_idx on public.lectures(replay_id);

-- ------------------------------------------------------------
-- Garde-fou : seul un e-mail présent et actif dans « apprenants »
-- peut recevoir un lien magique. Les autres sont refusés à la porte.
-- ------------------------------------------------------------
create or replace function public.verifier_apprenant()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if not exists (
    select 1 from public.apprenants
    where email = lower(new.email) and actif
  ) then
    raise exception 'Adresse non reconnue. Contacte DataSuits si tu penses que c''est une erreur.';
  end if;
  return new;
end;
$$;

drop trigger if exists apprenant_autorise on auth.users;
create trigger apprenant_autorise
  before insert on auth.users
  for each row execute function public.verifier_apprenant();

-- ------------------------------------------------------------
-- Règles d'accès (RLS) : un apprenant connecté ne voit que
-- les replays publiés de SA formation, et ses propres lectures.
-- Le script GitHub (clé service) contourne ces règles pour écrire.
-- ------------------------------------------------------------
alter table public.apprenants enable row level security;
alter table public.replays    enable row level security;
alter table public.lectures   enable row level security;

-- e-mail de l'utilisateur connecté, en minuscules
create or replace function public.mon_email()
returns text
language sql stable
as $$ select lower(auth.jwt() ->> 'email') $$;

-- apprenants : chacun voit uniquement sa propre fiche
drop policy if exists "ma fiche" on public.apprenants;
create policy "ma fiche" on public.apprenants
  for select to authenticated
  using (email = public.mon_email() and actif);

-- replays : publiés + même formation que l'apprenant connecté
drop policy if exists "replays de ma formation" on public.replays;
create policy "replays de ma formation" on public.replays
  for select to authenticated
  using (
    publie
    and exists (
      select 1 from public.apprenants a
      where a.email = public.mon_email()
        and a.actif
        and a.formation = replays.formation
    )
  );

-- lectures : j'écris et je lis uniquement les miennes
drop policy if exists "mes lectures (lecture)" on public.lectures;
create policy "mes lectures (lecture)" on public.lectures
  for select to authenticated
  using (email = public.mon_email());

drop policy if exists "mes lectures (écriture)" on public.lectures;
create policy "mes lectures (écriture)" on public.lectures
  for insert to authenticated
  with check (email = public.mon_email());

-- ------------------------------------------------------------
-- Fin. Vérification rapide : les 3 tables doivent apparaître
-- dans Table Editor, vides, avec le cadenas RLS activé.
-- ------------------------------------------------------------
