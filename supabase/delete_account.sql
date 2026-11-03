-- =====================================================================
-- AI Resume Analyzer: "Delete my account"
--
-- Run this once in the Supabase SQL Editor, AFTER schema.sql.
-- Safe to run again: the function is replaced.
--
-- delete_my_account() deletes the user who calls it from auth.users.
-- Every table in schema.sql references auth.users with ON DELETE CASCADE,
-- so the user's profile, analyses, saved jobs and usage rows go with it.
--
-- Why this is safe:
--   * It takes NO arguments. The user to delete always comes from
--     auth.uid(), which Supabase reads from the caller's signed login token.
--     Nobody can pass someone else's ID, so you can only delete yourself.
--   * Signed-out visitors can't call it (they have no auth.uid(), and
--     execute is granted to signed-in users only).
--   * SECURITY DEFINER is needed because normal users have no rights on
--     auth.users. The fixed, empty search_path stops anyone from tricking
--     the function into using a different "users" table.
-- =====================================================================

begin;

create or replace function public.delete_my_account()
returns void
language plpgsql
security definer
set search_path = ''   -- every name below is fully qualified
as $$
declare
  caller uuid := auth.uid();
begin
  if caller is null then
    raise exception 'You must be signed in to delete your account.';
  end if;

  delete from auth.users where id = caller;
end;
$$;

-- Only signed-in users may call it (by default every role could).
revoke execute on function public.delete_my_account() from public, anon;
grant  execute on function public.delete_my_account() to authenticated;

commit;
