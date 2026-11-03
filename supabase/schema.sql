-- =====================================================================
-- AI Resume Analyzer: database schema for Supabase
--
-- Run this whole file once in the Supabase SQL Editor.
-- It is safe to run again: tables are only created if missing, and
-- policies and functions are replaced.
--
-- Security model:
--   * The app uses the anon/publishable key, so EVERYONE could call the
--     database API. Row Level Security (RLS) is what keeps each user's
--     data private: every table has RLS on, and every rule checks
--     auth.uid() = user_id (the signed-in user can only see their own rows).
--   * Usage counts can only be read by users. The only way to change them
--     is the increment_usage() function, which can only ever add 1.
--   * The resume file and its full text are never stored.
-- =====================================================================

begin;

-- ---------------------------------------------------------------------
-- Helper: keep updated_at current on every update
-- ---------------------------------------------------------------------
create or replace function public.set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;


-- ---------------------------------------------------------------------
-- profiles: one row per user with their questionnaire answers
-- ---------------------------------------------------------------------
create table if not exists public.profiles (
  user_id    uuid primary key default auth.uid()
             references auth.users (id) on delete cascade,
  profile    jsonb not null default '{}'::jsonb,   -- the UserProfile as JSON
  updated_at timestamptz not null default now()
);

drop trigger if exists profiles_set_updated_at on public.profiles;
create trigger profiles_set_updated_at
  before update on public.profiles
  for each row execute function public.set_updated_at();


-- ---------------------------------------------------------------------
-- analyses: saved analysis results (NOT the resume file or its text)
-- ---------------------------------------------------------------------
create table if not exists public.analyses (
  id         bigint generated always as identity primary key,
  user_id    uuid not null default auth.uid()
             references auth.users (id) on delete cascade,
  file_name  text not null,
  word_count integer not null check (word_count >= 0),
  issues     jsonb not null default '[]'::jsonb,  -- rule-check issues
  ai_review  jsonb,                               -- null if no AI review was run
  created_at timestamptz not null default now()
);

create index if not exists analyses_user_created_idx
  on public.analyses (user_id, created_at desc);


-- ---------------------------------------------------------------------
-- saved_jobs: jobs a user bookmarked
-- ---------------------------------------------------------------------
create table if not exists public.saved_jobs (
  id       bigint generated always as identity primary key,
  user_id  uuid not null default auth.uid()
           references auth.users (id) on delete cascade,
  job      jsonb not null,                        -- the Job as JSON (must include "url")
  -- Copied automatically from job->>'url', so it can never disagree with the JSON.
  job_url  text generated always as (job ->> 'url') stored not null,
  score    integer not null check (score between 0 and 100),
  saved_at timestamptz not null default now(),
  -- The same job can only be saved once per user.
  constraint saved_jobs_one_per_user unique (user_id, job_url)
);


-- ---------------------------------------------------------------------
-- usage: how many searches and AI reviews each user did per day
-- ---------------------------------------------------------------------
create table if not exists public.usage (
  user_id    uuid not null references auth.users (id) on delete cascade,
  day        date not null default current_date,   -- UTC date on Supabase
  searches   integer not null default 0 check (searches >= 0),
  ai_reviews integer not null default 0 check (ai_reviews >= 0),
  primary key (user_id, day)
);


-- ---------------------------------------------------------------------
-- Row Level Security: on for every table
-- ---------------------------------------------------------------------
alter table public.profiles   enable row level security;
alter table public.analyses   enable row level security;
alter table public.saved_jobs enable row level security;
alter table public.usage      enable row level security;

-- Signed-out visitors (the "anon" role) get no access at all.
revoke all on public.profiles, public.analyses, public.saved_jobs, public.usage from anon;

-- profiles: read, create, change and delete your own row
drop policy if exists "profiles: read own"   on public.profiles;
drop policy if exists "profiles: insert own" on public.profiles;
drop policy if exists "profiles: update own" on public.profiles;
drop policy if exists "profiles: delete own" on public.profiles;
create policy "profiles: read own"   on public.profiles for select to authenticated
  using ((select auth.uid()) = user_id);
create policy "profiles: insert own" on public.profiles for insert to authenticated
  with check ((select auth.uid()) = user_id);
create policy "profiles: update own" on public.profiles for update to authenticated
  using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);
create policy "profiles: delete own" on public.profiles for delete to authenticated
  using ((select auth.uid()) = user_id);

-- analyses: read, add and delete your own. No update: a saved analysis is a record.
drop policy if exists "analyses: read own"   on public.analyses;
drop policy if exists "analyses: insert own" on public.analyses;
drop policy if exists "analyses: delete own" on public.analyses;
create policy "analyses: read own"   on public.analyses for select to authenticated
  using ((select auth.uid()) = user_id);
create policy "analyses: insert own" on public.analyses for insert to authenticated
  with check ((select auth.uid()) = user_id);
create policy "analyses: delete own" on public.analyses for delete to authenticated
  using ((select auth.uid()) = user_id);

-- saved_jobs: read, add and delete your own. No update: you save or unsave a job.
drop policy if exists "saved_jobs: read own"   on public.saved_jobs;
drop policy if exists "saved_jobs: insert own" on public.saved_jobs;
drop policy if exists "saved_jobs: delete own" on public.saved_jobs;
create policy "saved_jobs: read own"   on public.saved_jobs for select to authenticated
  using ((select auth.uid()) = user_id);
create policy "saved_jobs: insert own" on public.saved_jobs for insert to authenticated
  with check ((select auth.uid()) = user_id);
create policy "saved_jobs: delete own" on public.saved_jobs for delete to authenticated
  using ((select auth.uid()) = user_id);

-- usage: READ your own only. There are deliberately no insert, update or
-- delete policies, so with RLS on those are always refused. The table
-- privileges are removed as well, as a second lock on the same door.
drop policy if exists "usage: read own" on public.usage;
create policy "usage: read own" on public.usage for select to authenticated
  using ((select auth.uid()) = user_id);
revoke insert, update, delete, truncate on public.usage from authenticated;


-- ---------------------------------------------------------------------
-- increment_usage: the only way to change usage counts
--
-- Adds 1 to today's count for the signed-in user and returns true,
-- or returns false (and adds nothing) if the daily limit is reached.
--   kind:        'searches' or 'ai_reviews'
--   daily_limit: the app's limit, e.g. 10
--
-- SECURITY DEFINER means it runs with the owner's rights, so it can write
-- to usage even though users can't. It only ever touches the caller's own
-- row (auth.uid()) and only ever adds 1, so it can't be used to reset counts.
-- ---------------------------------------------------------------------
create or replace function public.increment_usage(kind text, daily_limit integer)
returns boolean
language plpgsql
security definer
set search_path = ''   -- no schema tricks: every name below is fully qualified
as $$
declare
  caller        uuid := auth.uid();
  current_count integer;
begin
  if caller is null then
    raise exception 'You must be signed in to use this feature.';
  end if;
  if kind is null or kind not in ('searches', 'ai_reviews') then
    raise exception 'Unknown usage kind: %', kind;
  end if;
  if daily_limit is null or daily_limit < 0 then
    raise exception 'daily_limit must be 0 or more.';
  end if;

  -- Make sure today's row exists.
  insert into public.usage (user_id, day)
  values (caller, current_date)
  on conflict (user_id, day) do nothing;

  -- Lock today's row, so two clicks at the same moment can't both slip under the limit.
  select case when kind = 'searches' then u.searches else u.ai_reviews end
    into current_count
    from public.usage as u
   where u.user_id = caller and u.day = current_date
     for update;

  if current_count >= daily_limit then
    return false;
  end if;

  update public.usage as u
     set searches   = u.searches   + (case when kind = 'searches'   then 1 else 0 end),
         ai_reviews = u.ai_reviews + (case when kind = 'ai_reviews' then 1 else 0 end)
   where u.user_id = caller and u.day = current_date;

  return true;
end;
$$;

-- Only signed-in users may call it (by default every role could).
revoke execute on function public.increment_usage(text, integer) from public, anon;
grant  execute on function public.increment_usage(text, integer) to authenticated;

commit;
