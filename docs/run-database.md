# Optional PostgreSQL index

Install `psycopg[binary]>=3.2,<4`, create a database owned by your application user, then:

```sh
python -m waam_pipeline.datastore --help
```

Supply the connection through `WAAM_PG_DSN` or `--dsn`. Migrate and ingest with a configured read role, or explicitly use `--read-role ''` when no reporting role is needed.

The full test suite requires PostgreSQL. Set `WAAM_TEST_PG_DSN` to a dedicated test database with a `waam_read` role, or set `WAAM_PG_BIN` to a folder containing `initdb` and `pg_ctl`. The latter starts a temporary server on localhost and removes it after testing. Windows binaries are available from [EDB](https://www.enterprisedb.com/download-postgresql-binaries). Without either setting, Linux tests try the local database `waam_test`.

## Query examples

```sql
SELECT asf, count(*) AS segments,
       round(avg(current_mean_a)::numeric, 1) AS amps,
       round(avg(voltage_mean_v)::numeric, 2) AS volts,
       round(avg(speed_mm_min)::numeric) AS mm_min,
       round(avg(heat_input_kj_mm)::numeric, 3) AS kj_mm
FROM runindex.arc_segments GROUP BY asf ORDER BY asf;
```

```sql
SELECT at, stage, round(value_c::numeric, 1) AS c, value || ' ' || unit AS entered, cycle, note
FROM runindex.temperatures WHERE run_id = '2026-10-06_LAYER-TOUCHV7_r01' ORDER BY at;
```

```sql
SELECT l.run_id, l.layer, l.pass, round(l.pre_weld_c::numeric, 1) AS pre_weld_c,
       r.seconds, round(r.value_c::numeric, 1) AS reading_c, l.height_mm, l.width_mm
FROM runindex.layer_log l LEFT JOIN runindex.layer_readings r USING (run_id, row_no)
ORDER BY l.run_id, l.layer, l.pass NULLS FIRST, r.seconds;
```

```sql
SELECT s.run_id, p.layer, s.asf, round(s.heat_input_kj_mm::numeric, 3) AS kj_mm, l.width_mm, l.height_mm
FROM runindex.arc_segments s
JOIN runindex.passes p ON p.export_id = s.export_id AND s.first_step BETWEEN p.first_step AND p.last_step
JOIN runindex.layer_log l ON l.run_id = s.run_id AND l.layer = p.layer AND (l.pass IS NULL OR l.pass = p.pass)
ORDER BY s.run_id, p.layer;

SELECT r.run_id, count(*) AS segments, round(avg(s.heat_input_kj_mm)::numeric, 3) AS kj_mm,
       (r.measurements->>'width_mm')::float AS width_mm, (r.measurements->>'height_mm')::float AS height_mm
FROM runindex.runs r JOIN runindex.arc_segments s USING (run_id)
WHERE r.measurements->>'width_mm' IS NOT NULL
GROUP BY r.run_id ORDER BY r.run_id;
```

```sql
SELECT date_trunc('week', started_at)::date AS week, count(*) AS runs,
       count(*) FILTER (WHERE outcome = 'completed') AS completed,
       count(*) FILTER (WHERE NOT sealed) AS open
FROM runindex.runs GROUP BY 1 ORDER BY 1;
```

```sql
SELECT coalesce(e.job, '(no weld export filed before it)') AS job, count(*) AS segments,
       round(sum(s.weld_s)::numeric, 1) AS weld_s, round(avg(s.current_mean_a)::numeric) AS amps,
       round(avg(s.speed_mm_min)::numeric) AS mm_min
FROM runindex.arc_segments s LEFT JOIN runindex.exports e ON e.id = s.export_id
GROUP BY 1 ORDER BY weld_s DESC;
```

```sql
SELECT run_id, layer, pass, round(pre_weld_c::numeric) AS pre_weld_c FROM runindex.layer_log WHERE pre_weld_c > 300
UNION ALL
SELECT run_id, NULL, NULL, round(value_c::numeric) FROM runindex.temperatures
WHERE stage IN ('before-weld', 'before-interpass') AND value_c > 300;
```

```sql
SELECT run_id, cycle, first_layer, last_layer, status, round(growth_mm::numeric, 2) AS growth_mm,
       round(mean_error_mm::numeric, 2) AS mean_err_mm, round(t_before_c::numeric) AS before_c,
       round(t_after_c::numeric) AS after_c, touches
FROM runindex.cycles ORDER BY run_id, cycle;
```
