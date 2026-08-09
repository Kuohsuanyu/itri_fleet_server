-- ITRI Fleet Console schema.  Idempotent: safe to run on every startup.
--
-- Design notes
--   * telemetry is FULL FIDELITY (every sample is stored, no downsampling).
--     At 50 robots x 2 Hz that is ~100 rows/s, ~41 GB/month -- cheap enough
--     that losing resolution to save disk would be a bad trade when the whole
--     point of the archive is post-incident forensics.
--   * telemetry is RANGE partitioned by day. Retention is then a DROP TABLE
--     per expired day: instant, no VACUUM churn, no bloat.
--   * events are low volume and kept forever -- they are the audit trail.

CREATE TABLE IF NOT EXISTS robots (
    id           text PRIMARY KEY,
    name         text        NOT NULL,
    secret_hash  text,                        -- sha256(secret); NULL until enrolled
    enrolled_at  timestamptz NOT NULL DEFAULT now(),
    revoked_at   timestamptz,
    last_seen    timestamptz,
    tags         text[]      NOT NULL DEFAULT '{}',
    display      jsonb       NOT NULL DEFAULT '{}'::jsonb,
    notes        text
);

-- One-time enrollment tokens handed out by the dashboard.
CREATE TABLE IF NOT EXISTS enroll_tokens (
    token       text PRIMARY KEY,
    robot_id    text,
    name        text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    used_at     timestamptz,
    used_by_ip  text
);
CREATE INDEX IF NOT EXISTS idx_enroll_expiry ON enroll_tokens (expires_at)
    WHERE used_at IS NULL;

-- The firehose.  No primary key on purpose: a PK would have to include the
-- partition key and would cost an index write per row for no query benefit.
CREATE TABLE IF NOT EXISTS telemetry (
    robot_id  text        NOT NULL,
    ts        timestamptz NOT NULL,
    battery   real,
    state     text,
    v         real,
    w         real,
    x         real,
    y         real,
    yaw       real,
    temp      real,
    wifi      real,
    odom      real,
    extra     jsonb
) PARTITION BY RANGE (ts);

-- Indexes declared on the parent propagate to every partition automatically.
--   btree : "robot X between T1 and T2" -- the normal forensic query
--   brin  : whole-fleet time scans, ~1/1000th the size of an equivalent btree
CREATE INDEX IF NOT EXISTS idx_tel_robot_ts ON telemetry (robot_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_tel_ts_brin  ON telemetry USING brin (ts);

-- Catches anything that arrives outside the managed partition range instead of
-- erroring the insert.  Should normally stay empty; monitored in /api/metrics.
CREATE TABLE IF NOT EXISTS telemetry_default PARTITION OF telemetry DEFAULT;

-- Topics relayed verbatim from each vehicle's own local broker by itri-agent.
--
-- `telemetry` above is the normalised view the dashboard renders. This is the
-- unfiltered record: whatever the onboard computer was already publishing, kept
-- under its original topic name. The agent does not know what any of it means,
-- which is the point -- adding a new chassis model needs no code.
--
-- `num` is filled when the payload is a bare number (the common case), so
-- charting reads one indexed column instead of extracting from jsonb.
CREATE TABLE IF NOT EXISTS topic_samples (
    robot_id  text        NOT NULL,
    ts        timestamptz NOT NULL,
    topic     text        NOT NULL,
    num       real,
    payload   jsonb,
    -- 0 = the value changed, 1 = an unchanged value resent as a heartbeat.
    -- Without this, on_change_only makes "reading is steady" and "sensor
    -- stopped reporting" produce exactly the same rows: none.
    flag      smallint    NOT NULL DEFAULT 0
) PARTITION BY RANGE (ts);

CREATE INDEX IF NOT EXISTS idx_tsm_robot_topic ON topic_samples (robot_id, topic, ts DESC);
CREATE INDEX IF NOT EXISTS idx_tsm_brin        ON topic_samples USING brin (ts);
CREATE TABLE IF NOT EXISTS topic_samples_default PARTITION OF topic_samples DEFAULT;

-- Which topics each robot has ever sent, so the admin UI can offer them for
-- mapping without scanning the whole archive.
CREATE TABLE IF NOT EXISTS topic_catalog (
    robot_id   text NOT NULL,
    topic      text NOT NULL,
    first_seen timestamptz NOT NULL DEFAULT now(),
    last_seen  timestamptz NOT NULL DEFAULT now(),
    samples    bigint      NOT NULL DEFAULT 0,
    last_value text,
    -- last_seen moves on every sample; last_changed only when the value
    -- actually differs. A topic with a recent last_seen and an old
    -- last_changed is healthy and steady; one where both are old is silent.
    last_changed timestamptz,
    PRIMARY KEY (robot_id, topic)
);

-- Audit trail / incident log.  Small, permanent.
CREATE TABLE IF NOT EXISTS events (
    id        bigserial PRIMARY KEY,
    robot_id  text,
    ts        timestamptz NOT NULL DEFAULT now(),
    kind      text        NOT NULL,   -- online|offline|state|error|enroll|revoke|login|cmd|system
    severity  text        NOT NULL DEFAULT 'info',   -- info|warn|critical
    detail    jsonb
);
CREATE INDEX IF NOT EXISTS idx_events_ts    ON events (ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_robot ON events (robot_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_kind  ON events (kind, ts DESC);

-- Alerting. Rules are data, edited in the dashboard, applied without a restart.
CREATE TABLE IF NOT EXISTS alert_rules (
    id          serial PRIMARY KEY,
    name        text    NOT NULL,
    enabled     boolean NOT NULL DEFAULT true,
    robot_id    text,                      -- NULL = every robot
    source      text    NOT NULL,          -- field | topic | presence
    key         text    NOT NULL,          -- 'battery' | 'chassis/bat_pct' | ''
    op          text    NOT NULL,          -- lt gt outside inside eq ne offline stale
    value       real,
    value2      real,                      -- upper bound for outside/inside
    text_value  text,                      -- for eq/ne against strings
    -- Debounce: the condition must hold this long before firing. Without it a
    -- single noisy sample pages someone at 3am.
    for_seconds real    NOT NULL DEFAULT 10,
    -- Hysteresis: clear at a different level than trigger, so a value sitting
    -- exactly on the threshold does not flap.
    clear_value real,
    severity    text    NOT NULL DEFAULT 'warn',
    cooldown_min real   NOT NULL DEFAULT 15,
    channels    text[],                    -- NULL = every enabled channel
    -- Custom notification text. Placeholders: {robot} {id} {key} {value}
    -- {limit} {limit2} {rule} {severity}. Empty = auto-generated wording.
    message_template text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Added after the first deploy; ALTER is a no-op when the column exists.
ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS message_template text;

CREATE TABLE IF NOT EXISTS alerts (
    id          bigserial PRIMARY KEY,
    rule_id     integer,
    rule_name   text        NOT NULL,
    robot_id    text        NOT NULL,
    severity    text        NOT NULL,
    message     text        NOT NULL,
    value       real,
    started_at  timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    notified    text[]                     -- channels that accepted it
);
CREATE INDEX IF NOT EXISTS idx_alerts_open  ON alerts (robot_id, started_at DESC)
    WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_alerts_time  ON alerts (started_at DESC);

-- Browsers/phones subscribed to Web Push. A PWA installed from the dashboard
-- can receive notifications with the screen off -- no app store, and no Apple
-- Developer fee, because iOS 16.4+ supports Web Push for home-screen web apps.
CREATE TABLE IF NOT EXISTS push_subscriptions (
    endpoint   text PRIMARY KEY,
    p256dh     text NOT NULL,
    auth       text NOT NULL,
    label      text,
    user_agent text,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_ok    timestamptz,
    failures   integer NOT NULL DEFAULT 0
);

-- Schema version, so a future migration knows what it is looking at.
CREATE TABLE IF NOT EXISTS schema_meta (
    key   text PRIMARY KEY,
    value text NOT NULL
);
INSERT INTO schema_meta (key, value) VALUES ('version', '2')
    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;


-- ---------------------------------------------------------------------------
-- Migrations
--
-- Everything above is CREATE TABLE IF NOT EXISTS, which does nothing at all to
-- a table that already exists -- including not adding new columns. A database
-- created before a column was introduced would keep working right up until the
-- first COPY that mentions it, and then every flush would fail.
--
-- So each added column gets an explicit, idempotent ALTER here. This whole file
-- is executed on every startup, so these must stay safe to re-run forever.
-- ---------------------------------------------------------------------------

-- v2: distinguish a value that changed from one resent as a heartbeat.
ALTER TABLE topic_samples ADD COLUMN IF NOT EXISTS flag smallint NOT NULL DEFAULT 0;
ALTER TABLE topic_catalog ADD COLUMN IF NOT EXISTS last_changed timestamptz;
