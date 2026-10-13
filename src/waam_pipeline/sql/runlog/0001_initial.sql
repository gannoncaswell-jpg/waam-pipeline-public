


CREATE TABLE runs (
    run_id       text COLLATE "C" PRIMARY KEY,
    workspace    text NOT NULL,
    operator     text NOT NULL,
    owner_key    text NOT NULL,
    name         text,
    part         text NOT NULL,
    status       text NOT NULL,
    outcome      text,
    data_class   text,
    started_at   text COLLATE "C" NOT NULL,
    finished_at  text,
    revision     text NOT NULL,
    source_json  text NOT NULL
);
CREATE INDEX runs_started ON runs (started_at DESC);

CREATE TABLE run_access (
    run_id  text COLLATE "C" NOT NULL REFERENCES runs ON DELETE CASCADE,
    email   text NOT NULL,
    PRIMARY KEY (run_id, email)
);
CREATE INDEX run_access_email ON run_access (email);

CREATE TABLE setup (
    run_id            text COLLATE "C" PRIMARY KEY REFERENCES runs ON DELETE CASCADE,
    specimen_id       text,
    substrate_id      text,
    material          text,
    wire_material     text,
    shielding_gas     text,
    arc_file          text,
    wire_diameter_mm  double precision,
    gas_flow_l_min    double precision,
    travel_mm_min     double precision,
    wire_feed_m_min   double precision,
    voltage_v         double precision,
    ctwd_mm           double precision
);

CREATE TABLE layers (
    run_id             text COLLATE "C" NOT NULL REFERENCES runs ON DELETE CASCADE,
    row_number         bigint NOT NULL,
    layer              bigint NOT NULL,
    pass               bigint,
    pass_base          bigint,
    unit               text NOT NULL,
    pre_weld           double precision,
    pre_weld_value_c   double precision,
    park_travel_s_est  double precision,
    height_mm          double precision,
    height_source      text,
    width_mm           double precision,
    note               text,
    at                 text,
    current_a          double precision,
    voltage_v          double precision,
    source_json        text NOT NULL,
    PRIMARY KEY (run_id, row_number)
);

CREATE TABLE layer_readings (
    run_id      text COLLATE "C" NOT NULL,
    row_number  bigint NOT NULL,
    seconds     double precision NOT NULL,
    value       double precision NOT NULL,
    unit        text NOT NULL,
    value_c     double precision NOT NULL,
    PRIMARY KEY (run_id, row_number, seconds),
    FOREIGN KEY (run_id, row_number) REFERENCES layers ON DELETE CASCADE
);

CREATE TABLE temperatures (
    run_id       text COLLATE "C" NOT NULL REFERENCES runs ON DELETE CASCADE,
    observation  bigint NOT NULL,
    at           text,
    value        double precision NOT NULL,
    unit         text NOT NULL,
    value_c      double precision NOT NULL,
    stage        text,
    note         text,
    cycle        bigint,
    PRIMARY KEY (run_id, observation)
);

CREATE TABLE audit (
    run_id  text COLLATE "C" NOT NULL REFERENCES runs ON DELETE CASCADE,
    event   bigint NOT NULL,
    actor   text NOT NULL,
    action  text NOT NULL,
    at      text NOT NULL,
    PRIMARY KEY (run_id, event)
);
