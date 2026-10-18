-- Mulligan's own log (mulligan-db, TimescaleDB). Objects are stored whole as their contract JSON
-- in `doc`; the other columns are copies for filtering and ordering.

CREATE TABLE sessions (
    id             text PRIMARY KEY,
    mcp_session_id text UNIQUE,
    agent_id       text NOT NULL,
    status         text NOT NULL,
    started_at     timestamptz NOT NULL,
    doc            jsonb NOT NULL
);
CREATE INDEX sessions_started_at ON sessions (started_at DESC);

CREATE TABLE actions (
    id         text NOT NULL,
    session_id text NOT NULL,
    system     text NOT NULL,
    status     text NOT NULL,
    created_at timestamptz NOT NULL,
    doc        jsonb NOT NULL,
    PRIMARY KEY (id, created_at)
);
SELECT create_hypertable('actions', by_range('created_at'));
CREATE INDEX actions_by_id ON actions (id);
CREATE INDEX actions_session ON actions (session_id, created_at);
CREATE INDEX actions_status ON actions (status, created_at);

-- Unredacted arguments of held calls, needed to run them after approval. Deleted once the call
-- runs or is rejected. Never exposed through the API.
CREATE TABLE held_calls (
    action_id  text PRIMARY KEY,
    tool       text NOT NULL,
    args       jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Checksum of the action's system just before it ran: a rewind of a run of actions is expected
-- to land on the oldest one's pre-checksum (plus any kept human edits).
CREATE TABLE action_checksums (
    action_id text PRIMARY KEY,
    system    text NOT NULL,
    pre       text NOT NULL,
    post      text
);

CREATE TABLE rewind_jobs (
    id         text PRIMARY KEY,
    status     text NOT NULL,
    created_at timestamptz NOT NULL,
    doc        jsonb NOT NULL
);
CREATE INDEX rewind_jobs_created_at ON rewind_jobs (created_at DESC);

CREATE TABLE events (
    seq   bigserial NOT NULL,
    ts    timestamptz NOT NULL,
    event text NOT NULL,
    data  jsonb NOT NULL,
    PRIMARY KEY (seq, ts)
);
SELECT create_hypertable('events', by_range('ts'));
CREATE INDEX events_seq ON events (seq);
