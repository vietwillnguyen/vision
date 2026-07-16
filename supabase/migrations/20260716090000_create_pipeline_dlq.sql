-- DLQ for storage keys the nightly pipeline could not process: rejected
-- (unparseable/out-of-prefix) keys and unmatched flag markers. The pipeline
-- retries pending rows each night with an attempt count and escalates to the
-- user once a key exhausts its retries (pipeline/pipeline/orchestrator.py).
create table public.pipeline_dlq (
  device_id uuid not null references public.devices(device_id) on delete cascade,
  key text not null,
  kind text not null check (kind in ('rejected', 'unmatched')),
  attempts integer not null default 1,
  escalated boolean not null default false,
  updated_at timestamptz not null default now(),
  primary key (device_id, key)
);

-- Only the pipeline (service_role) touches this table: RLS is enabled with no
-- policies, and no grants are issued to authenticated or anon.
alter table public.pipeline_dlq enable row level security;

grant select, insert, update, delete on public.pipeline_dlq to service_role;
