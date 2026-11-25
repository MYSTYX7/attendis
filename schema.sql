-- Run in Supabase SQL editor
create table if not exists guild_config (
  guild_id text primary key,
  min_minutes int not null default 15,
  timezone text not null default 'Asia/Kolkata',
  mode text not null default 'presence' check (mode in ('presence','voice','either')),
  log_channel_id text,
  reminder_minutes int not null default 10
);

create table if not exists moderators (
  id bigserial primary key,
  guild_id text not null,
  user_id text not null,
  name text not null,
  unique (guild_id, user_id)
);

create table if not exists slots (
  id bigserial primary key,
  guild_id text not null,
  moderator_id bigint not null references moderators(id) on delete cascade,
  user_id text not null,
  weekday smallint,            -- 0=Mon..6=Sun, null = not weekday-based
  slot_date date,              -- one-off date; if both null => every day
  start_time time not null,
  end_time time not null,
  check (start_time < end_time)
);

create table if not exists attendance (
  id bigserial primary key,
  guild_id text not null,
  slot_id bigint not null references slots(id) on delete cascade,
  moderator_id bigint not null references moderators(id) on delete cascade,
  user_id text not null,
  att_date date not null,
  minutes int not null default 0,
  status text not null default 'pending' check (status in ('pending','present','absent','excused')),
  method text,                 -- auto | manual
  marked_by text,
  note text,
  marked_at timestamptz,
  unique (slot_id, att_date)
);
create index if not exists att_guild_date on attendance (guild_id, att_date);
alter table guild_config enable row level security;
alter table moderators enable row level security;
alter table slots enable row level security;
alter table attendance enable row level security;
-- Bot uses the service_role key (bypasses RLS). Never expose that key publicly.

alter table guild_config add column if not exists leave_admin_id text;
alter table guild_config add column if not exists leave_channel_id text;
alter table guild_config add column if not exists leave_approvers text[] not null default '{}';

create table if not exists leaves (
  id bigserial primary key,
  guild_id text not null,
  moderator_id bigint not null references moderators(id) on delete cascade,
  user_id text not null,
  start_date date not null,
  end_date date not null,
  slot_ids bigint[],            -- null = whole day(s); otherwise only these slots (today)
  reason text,
  status text not null default 'pending' check (status in ('pending','approved','rejected','cancelled')),
  decided_by text,
  decided_at timestamptz,
  created_at timestamptz not null default now()
);
alter table leaves enable row level security;

-- Required on newer Supabase projects: let the bot's service_role use these tables
grant usage on schema public to service_role;
grant all on all tables in schema public to service_role;
grant all on all sequences in schema public to service_role;
alter default privileges in schema public grant all on tables to service_role;
alter default privileges in schema public grant all on sequences to service_role;
