-- Portfolio Positions ledger — operator-scoped position tracking for the Portfolio Risk Desk.
-- All data is owner-scoped via RLS: auth.uid() = user_id enforced on every operation.
-- PRD-R7: positions (entry price/shares) live only here; never committed to any repo artifact.
-- Make this idempotent: safe to re-run (create if not exists; drop-if-exists policies).

create table if not exists public.portfolio_positions (
  id           uuid        primary key default gen_random_uuid(),
  user_id      uuid        not null references auth.users(id) on delete cascade,
  ticker       text        not null,
  shares       numeric     null,
  entry_price  numeric     null,
  entry_date   date        null,
  notes        text        null,
  status       text        not null default 'open'
                           check (status in ('open', 'closed')),
  closed_at    timestamptz null,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

create index if not exists portfolio_positions_user_status
  on public.portfolio_positions (user_id, status);

alter table public.portfolio_positions enable row level security;

do $$
begin
  drop policy if exists portfolio_positions_owner_select on public.portfolio_positions;
  create policy portfolio_positions_owner_select
    on public.portfolio_positions for select
    using (auth.uid() = user_id);

  drop policy if exists portfolio_positions_owner_insert on public.portfolio_positions;
  create policy portfolio_positions_owner_insert
    on public.portfolio_positions for insert
    with check (auth.uid() = user_id);

  drop policy if exists portfolio_positions_owner_update on public.portfolio_positions;
  create policy portfolio_positions_owner_update
    on public.portfolio_positions for update
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

  drop policy if exists portfolio_positions_owner_delete on public.portfolio_positions;
  create policy portfolio_positions_owner_delete
    on public.portfolio_positions for delete
    using (auth.uid() = user_id);
end $$;
