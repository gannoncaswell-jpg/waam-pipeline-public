


CREATE TABLE runs (
    run_id          text PRIMARY KEY,
    workspace       text NOT NULL,
    part            text,
    name            text,
    operator        text,
    cell            text,
    git_commit      text,
    record_schema   text,
    status          text NOT NULL,
    outcome         text,
    data_class      text,
    started_at      timestamptz,
    finished_at     timestamptz,
    sealed          boolean NOT NULL DEFAULT false,
    sealed_at       timestamptz,
    seal_sha256     text,
    measurements    jsonb,
    arc_waiver      jsonb,
    plate           jsonb,
    record          jsonb NOT NULL,
    archive_path    text NOT NULL,
    content_sha256  text NOT NULL,
    ingest_version  integer NOT NULL,
    ingested_at     timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX runs_started_at ON runs (started_at);

CREATE TABLE run_files (
    run_id       text NOT NULL REFERENCES runs ON DELETE CASCADE,
    name         text NOT NULL,
    seq          integer,
    kind         text NOT NULL,
    sha256       text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes   bigint NOT NULL,
    mtime        timestamptz,
    added_at     timestamptz,
    source       text,
    parse_error  text,
    PRIMARY KEY (run_id, name)
);
CREATE INDEX run_files_sha256 ON run_files (sha256);

CREATE TABLE exports (
    id              bigserial PRIMARY KEY,
    run_id          text NOT NULL REFERENCES runs ON DELETE CASCADE,
    file_name       text NOT NULL,
    job_file        text,
    seq             integer NOT NULL,
    kind            text NOT NULL,
    job             text,
    exported_at     timestamptz,
    git_commit      text,
    arc_schedule    text,
    asf             integer[],
    weld_ctwd_mm    double precision,
    clean_first     boolean,
    layers          integer[],
    thermal         jsonb,
    measure         jsonb,
    platter         jsonb,
    params          jsonb,
    query           jsonb,
    summary         jsonb,
    cycle           jsonb,
    jbi_sha256      text,
    jbi_matches     boolean,
    sidecar_sha256  text NOT NULL,
    inputs_sha256   text NOT NULL,
    UNIQUE (run_id, file_name)
);
CREATE INDEX exports_job ON exports (job);

CREATE TABLE passes (
    export_id       bigint NOT NULL REFERENCES exports ON DELETE CASCADE,
    pass            integer NOT NULL,
    layer           integer,
    bead            integer,
    region          text,
    asf             integer,
    aef             integer,
    v_min_mm_s      double precision,
    v_max_mm_s      double precision,
    travel_mm_min   double precision,
    length_mm       double precision,
    weld_moves      integer,
    first_step      integer,
    last_step       integer,
    dwell_s         double precision,
    pauses_before   integer,
    expected_s      double precision,
    model_area_mm2  double precision,
    PRIMARY KEY (export_id, pass)
);
CREATE INDEX passes_asf ON passes (asf);


CREATE TABLE arcmon_files (
    id                 bigserial PRIMARY KEY,
    sha256             text NOT NULL UNIQUE CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    run_id             text NOT NULL REFERENCES runs ON DELETE CASCADE,
    file_name          text NOT NULL,
    format             text NOT NULL CHECK (format IN ('realtime-csv', 'arcmon-dat')),
    export_id          bigint REFERENCES exports ON DELETE SET NULL,
    export_match       text,
    job                text,
    controller_date    timestamp,
    sample_period_s    double precision,
    samples            integer NOT NULL DEFAULT 0,
    arc_on_a           double precision,
    merge_gap_samples  integer,
    has_step           boolean,
    channel_map        jsonb,
    summary            jsonb
);
CREATE INDEX arcmon_files_run ON arcmon_files (run_id);


CREATE TABLE arcmon_samples (
    file_id         bigint NOT NULL REFERENCES arcmon_files ON DELETE CASCADE,
    idx             integer NOT NULL,
    t_s             double precision NOT NULL,
    volts           double precision,
    amps            double precision,
    step            integer,
    cond_no         integer,
    weld_length_mm  double precision,
    other           jsonb,
    PRIMARY KEY (file_id, idx)
);
CREATE INDEX arcmon_samples_t_brin ON arcmon_samples USING brin (file_id, t_s);

CREATE TABLE temperatures (
    run_id   text NOT NULL REFERENCES runs ON DELETE CASCADE,
    seq      integer NOT NULL,
    at       timestamptz,
    value    double precision,
    unit     text,
    value_c  double precision,
    stage    text,
    note     text,
    cycle    integer,
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE notes (
    run_id  text NOT NULL REFERENCES runs ON DELETE CASCADE,
    seq     integer NOT NULL,
    at      timestamptz,
    note    text,
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE layer_log (
    run_id             text NOT NULL REFERENCES runs ON DELETE CASCADE,
    row_no             integer NOT NULL,
    layer              integer NOT NULL,
    pass               integer,
    unit               text,
    pre_weld           double precision,
    pre_weld_c         double precision,
    park_travel_s_est  double precision,
    height_mm          double precision,
    height_source      text,
    width_mm           double precision,
    note               text,
    at                 timestamptz,
    PRIMARY KEY (run_id, row_no)
);

CREATE TABLE layer_readings (
    run_id   text NOT NULL,
    row_no   integer NOT NULL,
    seconds  double precision NOT NULL,
    value    double precision,
    value_c  double precision,
    PRIMARY KEY (run_id, row_no, seconds),
    FOREIGN KEY (run_id, row_no) REFERENCES layer_log ON DELETE CASCADE
);

CREATE TABLE cycles (
    run_id           text NOT NULL REFERENCES runs ON DELETE CASCADE,
    cycle            integer NOT NULL,
    first_layer      integer,
    last_layer       integer,
    n                integer,
    status           text,
    job              text,
    file_name        text,
    var_file         text,
    step_mm          double precision,
    growth_raw_mm    double precision,
    growth_mm        double precision,
    clamped          boolean,
    mean_error_mm    double precision,
    min_error_mm     double precision,
    max_error_mm     double precision,
    touches          integer,
    spacing_mm       double precision,
    interpass_s      double precision,
    exported_at      timestamptz,
    imported_at      timestamptz,
    applied_at       timestamptz,
    t_before_c       double precision,
    t_after_c        double precision,
    bead_height_mm   double precision,
    bead_width_mm    double precision,
    travel_mm_min    double precision,
    wire_feed_m_min  double precision,
    weld_ctwd_mm     double precision,
    summary          jsonb,
    record           jsonb,
    PRIMARY KEY (run_id, cycle)
);

CREATE TABLE var_files (
    id         bigserial PRIMARY KEY,
    sha256     text NOT NULL UNIQUE CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    run_id     text NOT NULL REFERENCES runs ON DELETE CASCADE,
    file_name  text NOT NULL,
    kind       text NOT NULL,
    b_values   smallint[]
);
CREATE INDEX var_files_run ON var_files (run_id);


CREATE TABLE var_positions (
    var_file_id  bigint NOT NULL REFERENCES var_files ON DELETE CASCADE,
    p_index      integer NOT NULL,
    pulse_s      integer NOT NULL,
    pulse_l      integer NOT NULL,
    pulse_u      integer NOT NULL,
    pulse_r      integer NOT NULL,
    pulse_b      integer NOT NULL,
    pulse_t      integer NOT NULL,
    hit_flag     smallint,
    PRIMARY KEY (var_file_id, p_index)
);


CREATE TABLE thermocouple_samples (
    run_id   text REFERENCES runs ON DELETE CASCADE,
    sensor   text NOT NULL,
    at       timestamptz NOT NULL,
    value_c  double precision NOT NULL,
    PRIMARY KEY (sensor, at)
);
CREATE INDEX thermocouple_samples_at_brin ON thermocouple_samples USING brin (at);

CREATE TABLE ingest_runs (
    id              bigserial PRIMARY KEY,
    started_at      timestamptz NOT NULL,
    finished_at     timestamptz NOT NULL,
    saves           text NOT NULL,
    runs_seen       integer NOT NULL,
    runs_new        integer NOT NULL,
    runs_updated    integer NOT NULL,
    runs_unchanged  integer NOT NULL,
    runs_refused    integer NOT NULL,
    problems        jsonb NOT NULL
);


CREATE TABLE arcmon_segments (
    file_id                bigint NOT NULL REFERENCES arcmon_files ON DELETE CASCADE,
    segment                integer NOT NULL,
    start_s                double precision NOT NULL,
    end_s                  double precision NOT NULL,
    first_step             integer,
    last_step              integer,
    asf                    integer,
    aef                    integer,
    arc_on_s               double precision NOT NULL,
    weld_s                 double precision NOT NULL,
    dropouts               integer NOT NULL,
    current_mean_a         double precision,
    current_min_a          double precision,
    current_max_a          double precision,
    voltage_mean_v         double precision,
    voltage_min_v          double precision,
    voltage_max_v          double precision,
    arc_current_mean_a     double precision,
    arc_voltage_mean_v     double precision,
    crater_current_mean_a  double precision,
    weld_length_mm         double precision,
    speed_mm_min           double precision,
    heat_input_kj_mm       double precision,
    PRIMARY KEY (file_id, segment)
);


CREATE VIEW arc_segments AS
SELECT f.run_id, s.file_id, f.file_name, f.job, f.export_id, f.controller_date,
       s.segment, s.start_s, s.end_s, s.first_step, s.last_step, s.asf, s.aef,
       s.arc_on_s, s.weld_s, s.dropouts,
       s.current_mean_a, s.current_min_a, s.current_max_a,
       s.voltage_mean_v, s.voltage_min_v, s.voltage_max_v,
       s.arc_current_mean_a, s.arc_voltage_mean_v, s.crater_current_mean_a,
       s.weld_length_mm, s.speed_mm_min, s.heat_input_kj_mm
FROM arcmon_segments s
JOIN arcmon_files f ON f.id = s.file_id;


CREATE TABLE ingest_attempts (
    run_id          text PRIMARY KEY,
    content_sha256  text NOT NULL,
    attempts        integer NOT NULL,
    started_at      timestamptz NOT NULL
);
