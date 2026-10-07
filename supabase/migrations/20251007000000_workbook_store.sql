-- Hosted storage for the UH Homes selections workbook.
--
-- The workbook itself stays a single .xlsx in Storage. Every save uploads a new immutable
-- object and then advances uh_workbook_state by compare-and-swap, so two app processes can
-- never both win a save. Earlier objects are the saved copies the app offers to restore.

create table if not exists uh_workbook_state (
    id          integer primary key default 1 check (id = 1),
    revision    text        not null,
    object_path text        not null,
    updated_at  timestamptz not null default now()
);

comment on table uh_workbook_state is
    'Single pointer row naming the current workbook revision. Updated only by filtered compare-and-swap.';

create table if not exists uh_workbook_versions (
    revision    text primary key,
    name        text        not null unique,
    object_path text        not null,
    size        bigint      not null default 0,
    note        text,
    created_at  timestamptz not null default now()
);

comment on column uh_workbook_versions.object_path is
    'Empty once the object has been pruned; the row is kept so the history stays complete.';

create index if not exists uh_workbook_versions_created_at_idx
    on uh_workbook_versions (created_at desc);

create table if not exists uh_history (
    id    bigserial primary key,
    ts    timestamptz not null default now(),
    note  text        not null,
    actor text
);

create index if not exists uh_history_note_idx on uh_history (note text_pattern_ops);

create table if not exists uh_lookup_state (
    key        text primary key,
    content    text        not null,
    updated_at timestamptz not null default now()
);

-- The app reaches Postgres with the service key, which bypasses RLS. Enabling RLS with no
-- policies means the anon and authenticated keys cannot touch these tables even if leaked.
alter table uh_workbook_state    enable row level security;
alter table uh_workbook_versions enable row level security;
alter table uh_history           enable row level security;
alter table uh_lookup_state      enable row level security;

-- Private bucket for the workbook objects; only the service key can read or write it.
insert into storage.buckets (id, name, public)
values ('workbooks', 'workbooks', false)
on conflict (id) do nothing;
