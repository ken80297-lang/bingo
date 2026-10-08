-- Planned schema only: do not apply to production until worker integration is reviewed.
-- Private shadow records, no anon/authenticated grants.
create table if not exists public.external_ai_shadow_predictions (
  id bigint generated always as identity primary key,
  based_on_issue text not null,
  prediction_issue text not null,
  provider text not null,
  model text not null,
  recommend_numbers jsonb not null,
  top5 jsonb not null,
  super_number integer not null check (super_number between 1 and 80),
  generated_at timestamptz not null default now(),
  verified_at timestamptz,
  actual_numbers jsonb,
  actual_super_number integer,
  hit_count integer,
  top5_hit_count integer,
  super_hit boolean,
  status text not null default 'pending',
  unique (prediction_issue, provider, model)
);
alter table public.external_ai_shadow_predictions enable row level security;
revoke all on public.external_ai_shadow_predictions from anon, authenticated;
create index if not exists external_ai_shadow_pending_idx
on public.external_ai_shadow_predictions (prediction_issue) where verified_at is null;
