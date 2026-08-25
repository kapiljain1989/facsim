# Pharmaceutical Development and Manufacturing Simulator

A stateful, event-driven, causally coherent simulation of a pharmaceutical
programme end to end: the analytical laboratory that develops and validates the
methods, the clinical trial that tests the drug, and the plant that makes it.

It is not a random-data generator. Every layer is downstream of the one above it,
and that holds across domains as well as within them. A degrading bearing shifts
the sensor stream, the shifted stream shifts the recorded process parameters, and
the QC result moves because of it — and that QC result's precision is bounded by
the precision the analytical method demonstrated in its validation, while the
batch it released is the batch packed into the kit a trial subject was dispensed.

Three domains and the joins between them:

| | |
|---|---|
| 🏭 **Manufacturing** | sensor telemetry, batches, QC, failures, maintenance, deviations, RCA and CAPA. The deepest-modelled domain and the rest of this README is mostly about it |
| 🔬 **Analytical development** | chromatograms as signal traces, peak integration, ICH Q2 method validation, ICH Q1A stability with a fitted shelf life. [Documentation](docs/ANALYTICAL_DEVELOPMENT.md) |
| 🧪 **Clinical development** | a randomised oncology trial: RECIST lesion model with dual reader assessment, EDC, CTMS, eTMF, database lock, and SDTM/ADaM output. [Documentation](docs/CLINICAL_DEVELOPMENT.md) |
| 🔗 **The lifecycle spine** | the joins, and thirteen checks that walk them. [The plan](docs/LIFECYCLE_EXTENSION.md) |

> **This is a simulation and synthetic-data platform, not a GMP
> production-control system, and not a regulatory record.** No claim of
> compliance is made or implied. The investigational molecule and the clinical
> study are fictional; no real patient data appears anywhere. Suitable for
> research, software development, AI/ML testing, demonstrations and analytics.

---

## Contents

- [Quick start](#quick-start)
- [The three domains](#the-three-domains)
- [The lifecycle spine](#the-lifecycle-spine)
- [Are the numbers the right numbers?](#are-the-numbers-the-right-numbers)
- [What it produces](#what-it-produces)
- [The three design principles](#the-three-design-principles)
- [Configuration](#configuration)
- [Storage](#storage)
- [The live feed](#the-live-feed)
- [Dashboard and API](#dashboard-and-api)
- [CLI reference](#cli-reference)
- [Data volumes](#data-volumes)
- [Datasets and their columns](#datasets-and-their-columns)
- [Modelling notes and limitations](#modelling-notes-and-limitations)
- [Project layout](#project-layout)
- [Testing](#testing)

---

## Quick start

Needs Python 3.12+. No database or broker required for the default path.

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"

.venv/bin/python -m pharma_sim validate          # lint the configuration
.venv/bin/python -m pharma_sim init              # build and persist the factory
.venv/bin/python -m pharma_sim run --days 30     # generate a month of history
.venv/bin/python -m pharma_sim export --output data/export
```

Or in one step:

```bash
.venv/bin/python scripts/generate_dataset.py --days 30 --output data/export
```

Watch the plant run as a live feed, piped into anything:

```bash
.venv/bin/python -m pharma_sim run --live --speed 60 --sink jsonl | head -20
```

---

## The three domains

Each generates its own dataset with one command, and each writes a dataset card
alongside the data.

```bash
# Manufacturing — the plant
.venv/bin/python scripts/generate_dataset.py --days 45 --output data/plant

# Analytical development — method validation and stability
.venv/bin/python scripts/generate_lab_dataset.py --output data/lab \
    --manufacturing-export data/plant

# Clinical development — the trial
.venv/bin/python scripts/generate_clinical_dataset.py --output data/clinical \
    --manufacturing-export data/plant --lab-export data/lab
```

Run in that order and the domains link up. Run any one alone and it still
produces a complete, internally consistent dataset — but it says so. A clinical
dataset generated without a plant export labels every batch `STUB`, and a lot
whose expiry came from a configured constant rather than a fitted shelf life
records `DECLARED` rather than `STABILITY`. Those are different claims and the
data distinguishes them, because a lineage claim that silently degrades is worse
than none.

| Domain | What comes out | Documentation |
|---|---|---|
| Manufacturing | ~6,000 batches, ~38,000 QC results, telemetry, failures, deviations, RCA, CAPA | this README, and [configuring a factory](docs/CONFIGURING_A_FACTORY.md) |
| Analytical | 243 injections, 586 peaks, 1.09 M chromatogram points, 49 evaluated ICH Q2 criteria, 45 stability samples, a fitted 27-month shelf life | [ANALYTICAL_DEVELOPMENT.md](docs/ANALYTICAL_DEVELOPMENT.md) |
| Clinical | 120 subjects, 6 sites, 5,900 case report forms, 30,000 item values, SDTM TU/TR/RS, ADaM ADTTE, eTMF at 92% complete | [CLINICAL_DEVELOPMENT.md](docs/CLINICAL_DEVELOPMENT.md) |

---

## The lifecycle spine

The joins are the part nobody's demo data has, and they are checked rather than
asserted:

```
 SUBSTANCE  Nelvorasib (fictional, INN-stem conforming)
     |
     +-- METHOD  MTH-0001, validated to ICH Q2(R2)
     |      |
     |      +-- bounds the precision of the plant's QC result
     |      +-- measures every stability timepoint
     |
     +-- DRUG PRODUCT  active tablet + matching placebo
            |
            +-- made in a dedicated containment suite (OEB 4)
            +-- BATCH --- QP released ---+
                   |                     |
                   +-- STABILITY --> fitted shelf life --> LOT EXPIRY
                   |
                   +-- IMP LOT -> KIT -> SHIPMENT -> SITE -> SUBJECT DOSE
                                                              |
                                                        SDTM EX -> eCTD
```

`verify-spine` walks all of it. Thirteen checks, ordered by consequence — the
first is that a subject received the treatment they were randomised to, because
nothing else matters if that fails. Each check has a test confirming it fails
against a graph broken in that one way.

```
spine integrity (manufacturing batches)
  ok    dispensed product matches arm            1,519 checked
  ok    each kit dispensed once                  1,519 checked
  ok    kit resolves to lot and batch            1,920 checked
  ok    not dispensed before receipt             1,519 checked
  ok    no expired kit dispensed                 1,519 checked
  ...
  13/13 checks passed
```

### Are the numbers the right numbers?

Integrity checks say every row resolves. That is not the same as the data being
plausible, so there is a second gate:

```bash
.venv/bin/python scripts/verify_realism.py --plant data/plant/export \
    --lab data/lab --clinical data/clinical
```

Thirty-two metrics across the three domains and the spine, each checked against
an envelope declared in [`config/realism.yaml`](config/realism.yaml) with a
written reason. A value outside its envelope is a failure, and a metric that
cannot be computed at all is also a failure — a check that silently stops running
draws no attention to itself.

Some envelopes come from published ranges: objective response rate per arm,
median progression-free survival, investigator-versus-central-review discordance,
system suitability precision. Others exist to catch a specific regression, and
say so. The one guarding the exposure–response relationship is there because the
first version of that model **inverted** it — slowing only the tumour shrinkage
gave a shallower nadir, and since RECIST judges progression relative to the
nadir, less drug produced better survival. Nothing in the code looked wrong. That
envelope is what would catch it coming back.

---

## What it produces

The manufacturing domain, over a 30-day run of the shipped configuration:

| | |
|---|---|
| Plant | 1 plant, 14 production units, 104 machines — ten commercial units and a four-unit containment suite for the oncology programme |
| People | 1 plant manager, 10 unit managers, 100 unit workers, 8 technicians, 4 QC analysts |
| Telemetry | ~25 M sensor readings at the default 60 s cadence |
| Production | ~90 shift instances, ~9,000 production records with OEE, ~10,000 OEE snapshots |
| Batches | ~700 completed across 7 products, with full stage-by-stage genealogy |
| Quality | ~9,000 QC results, computed from achieved process conditions |
| Reliability | ~50 degradation episodes, ~35 faults, some averted by maintenance, ~300 maintenance actions |
| Quality management | ~250 deviations, ~50 RCA investigations, ~50 CAPAs with verification |
| Evaluation | ~50 hidden ground-truth events, ~145,000 forward-looking labels |

Every one of those rows resolves to a real machine, sensor, unit, plant, batch
and product — checked by `pharma_sim verify-integrity`, including across the
boundary between the relational and time-series stores.

---

## The three design principles

### 1. No factory vocabulary in Python

Every domain term is data loaded into a runtime registry, never a Python enum or
literal:

| Declared in YAML | Engine never says |
|---|---|
| machine states, transitions, **state roles** | `if state == "RUNNING"` |
| event types, severities, payload contracts | `EventType.MACHINE_STARTED` |
| equipment classes and their sensor bindings | `if machine_type == "tablet_press"` |
| sensor profiles, tags, noise models, rates | hard-coded tag lists |
| units, process stages, products, routes | ten named units |
| failure modes, precursors, hazard curves | `BEARING_FAILURE` constant |
| QC parameters, limits, transfer functions | hard-coded spec limits |

The load-bearing part is **semantic roles**. `states.yaml` declares what states
*mean* — `productive`, `downtime`, `planned_stop`, `requires_operator` — and
production, OEE and downtime logic read those roles. Rename `RUNNING`, add a
`QUALIFICATION` state, or replace the whole state model, and everything
downstream keeps working.

`config/examples/minimal_factory/` proves it: a two-unit liquids plant with five
machines, its own five-state model (no cleaning, changeover or starting state),
different equipment, different tags and two twelve-hour shifts. It runs on the
same engine with no code changes:

```bash
.venv/bin/python -m pharma_sim --config config/examples/minimal_factory run --hours 24
```

Because the type system cannot catch a reference to a state or tag that does not
exist, `pharma_sim validate` replaces that safety net. It checks every
cross-file reference — transition targets, sensor profiles, QC transfer inputs,
precursor tags, product routes, deviation triggers, MQTT topic placeholders — and
reports each problem with its file, YAML path and a suggested fix.

### 2. Causality is generated, never scripted

```
hazard model fires (rare, stochastic, driven by age/hours/debt/load/environment/operator)
  → failure mode initiates, incubation sampled, fault instant SCHEDULED
  → health index climbs a degradation curve
  → precursor tags move together: vibration ↑ → motor current ↑ → temperature ↑
  → machine enters its configured warning state
  → production rate ↓, reject rate ↑
  → process parameters drift (compression force ↑)
  → FAULT at the scheduled instant → production stops
  → QC computed from the achieved process values → hardness fails → batch rejected
  → deviation → corrective maintenance → RCA → CAPA → verification batches → closed
  → hidden ground truth and forward-looking labels written to an isolated store
```

Nothing in that chain is a coin flip on the outcome. Each arrow is a model.

**Failures are random and rare, but never a flat probability check.** For each
machine and applicable mode:

```
λ(t) = weibull(mode_age; mtbf, β) × age × operating_hours × maintenance_debt
       × load × environment × operator × hazard_scale
P(initiate in Δt) = 1 − exp(−λ·Δt)
```

β > 1 gives wear-out; β = 1 gives a memoryless mode such as a grid interruption.
The Weibull clock is **per mode and resets when that mode is repaired** — this is
a repairable system, not a component running to first failure since
commissioning — and each machine's clock is seeded across its wear cycle so the
fleet is not synchronised.

**The fault instant is fixed at onset.** That is what makes an observable
precursor window possible and what makes remaining-useful-life labels exact
rather than estimated. If maintenance intervenes first, the episode is marked
**averted**, and its labels say so — otherwise they would assert failures that
never happened.

**QC is computed, never sampled independently.** `qc_rules.yaml` declares
transfer functions, so quality is a consequence:

```
compression_force, moisture → hardness → disintegration → dissolution
inlet_temperature, drying_time → moisture → hardness
blend_time, rpm → blend uniformity → content uniformity → assay
```

The inputs are the *measured* stage means from the telemetry the machine actually
produced, so a QC result is arithmetically downstream of the sensor stream.

**Sensor data is composed, not drawn from a range.** Each tag is:

```
baseline(state role, product setpoint)
  + AR(1) fluctuation, time-scaled so autocorrelation is cadence-correct
  + Gaussian measurement noise
  + bounded random-walk drift
  + diurnal and ambient coupling
  + health-driven wear response
  + failure-specific precursor offsets
  + instrument malfunction: stuck-at, dropout, spike, noise burst
```

Correlation between tags is *structural*, not an imposed matrix: shared latent
drivers — health index, load, ambient conditions — feed multiple tags, so
vibration, current and temperature rise together because the same condition
drives all three. Every reading carries a `quality` field
(`GOOD`/`UNCERTAIN`/`BAD`) as real OT data does.

**Reproducibility.** No `uuid4`, no `time.time()`, no unseeded `random` in any
simulation path. Each entity draws from its own named stream derived from the
master seed (`Random(f"{seed}:sensor:TP-006:vibration")`), so a stream is
identical regardless of iteration order or interleaving. Two runs at the same
seed produce byte-identical event streams; the test suite asserts it.

### 3. A different store for each data shape

| Purpose | Structure | Backend |
|---|---|---|
| Transactional business records | normalised, FK-enforced, indexed, JSONB payloads | **PostgreSQL** (`oltp` schema) or **SQLite** |
| High-frequency telemetry | narrow, append-only, columnar, compressed, no FKs | **ClickHouse**, **TimescaleDB** or **Parquet** |
| Evaluation data | isolated, never joinable into operational queries | separate schema / Parquet directory |
| Live feed | append-only message stream | **MQTT**, **JSONL** |
| RCA lookback | bounded in-memory ring buffers | runtime only |

Measured on a 2-day run: ClickHouse stored 1.68 M readings in **6.5 MB —
4.0 bytes per reading** — with per-minute rollups maintained on insert.

---

## Configuration

Everything lives in `config/`. Nothing about the factory is hard-coded.

Building a different factory — new equipment, a new product, a different
plant entirely? [docs/CONFIGURING_A_FACTORY.md](docs/CONFIGURING_A_FACTORY.md)
is the walkthrough: the dependency order to write files in, a worked example,
and the gotchas that aren't obvious from the schema (WIP sizing, duty classes,
alarm headroom, incubation windows). This section is the reference table.

| File | Declares |
|---|---|
| `plant.yaml` | identity, timezone, seed, cadences, ambient environment |
| `states.yaml` | machine states, legal transitions, semantic roles |
| `event_types.yaml` | event vocabulary, severities, required payload fields |
| `units.yaml` | production units, process stages, staffing, roles |
| `machines.yaml` | equipment classes, sensor bindings, plant layout |
| `sensors.yaml` | reusable sensor profiles and their stochastic models |
| `shifts.yaml` | shift patterns, breaks, absenteeism, overtime |
| `products.yaml` | product catalogue, routes, setpoints, QC specifications |
| `qc_rules.yaml` | QC parameters, limits and transfer functions |
| `failures.yaml` | failure modes, hazard model, precursor signatures, effects |
| `maintenance.yaml` | PM policy, deferral, predictive response, technicians |
| `rca_rules.yaml` | evidence rules and root-cause rules with 5-Why chains |
| `deviations.yaml` | which events open a deviation, and what it requires |
| `scenarios.yaml` | predefined scenarios |
| `sinks.yaml` | streaming sinks, MQTT topics, queue bounds |
| `storage.yaml` | which backend holds which data shape |

Change unit counts, worker counts, machine counts, shift timings, simulation
speed, sampling frequency, failure rates or production rates without touching
Python.

### Sensor binding

No equipment-class-to-profile mapping is imposed. A class may inherit a profile,
patch it, remove tags from it, declare its sensors inline, or any combination:

```yaml
equipment_classes:
  - id: tablet_press
    sensor_profile: tablet_press          # inherit
    sensors:
      - {tag: turret_speed, baseline: 60, unit: rpm, rate_s: 1}   # add
      - {tag: hardness, override: {sigma: 0.4}}                   # patch
      - {tag: legacy_tag, remove: true}                           # drop

  - id: cartoner                          # no profile at all
    sensors: [{tag: line_speed, baseline: 120, unit: ppm, rate_s: 5}]
```

The shipped configuration has 33 equipment classes sharing 10 sensor profiles,
with 13 classes defined entirely inline.

### Editor support

```bash
.venv/bin/python -m pharma_sim schema --output schemas/
```

Emits JSON Schema for every config file, for autocomplete and validation in an
editor.

### Environment overrides

For containers and CI, a small set of deployment-only overrides:
`PHARMA_SEED`, `PHARMA_SENSOR_INTERVAL_S`, `PHARMA_TRANSACTIONAL_BACKEND`,
`PHARMA_TRANSACTIONAL_DSN`, `PHARMA_TIMESERIES_BACKEND`, `PHARMA_TIMESERIES_DSN`,
`PHARMA_EVALUATION_BACKEND`, `PHARMA_EVALUATION_DSN`, `PHARMA_MQTT_HOST`.
Anything that changes the factory's *behaviour* stays in version-controlled YAML.

---

## Storage

The default needs no infrastructure:

```yaml
transactional: {backend: sqlite,  dsn: ./data/factory.db}
timeseries:    {backend: parquet, dsn: ./data/telemetry, partition_by: [date, unit_id]}
evaluation:    {backend: parquet, dsn: ./data/eval}
```

For the production shape:

```bash
docker compose up -d postgres clickhouse mosquitto
uv pip install -e ".[dev,postgres,clickhouse]"
```

then in `config/storage.yaml`:

```yaml
transactional: {backend: postgres,   dsn: postgresql://pharma:pharma@localhost:5432/pharma}
timeseries:    {backend: clickhouse, dsn: clickhouse://default@localhost:9000/pharma_ts}
evaluation:    {backend: postgres,   dsn: postgresql://pharma:pharma@localhost:5432/pharma, schema_name: eval}
```

`timeseries.backend: timescale` is also implemented, and uses the same Postgres
instance — worth choosing when you would rather join telemetry to batches in
plain SQL than have ClickHouse's scan speed.

### Referential integrity across a polyglot boundary

Foreign keys are enabled in both relational backends, so an orphan row is
rejected by the database. A ClickHouse or Parquet telemetry row cannot have a
foreign key into Postgres, so that guarantee is enforced deliberately instead:

1. IDs originate in the config registries, and both stores are populated from the
   same resolved objects — never from each other.
2. The telemetry writer rejects any reading whose `sensor_id` is not in the
   loaded registry, so an orphan cannot be written.
3. The sensor dimension is mirrored into the time-series store, so telemetry is
   self-describing there too.
4. `pharma_sim verify-integrity` cross-checks the boundary explicitly.

```
$ python -m pharma_sim verify-integrity
  [PASS] every telemetry machine_id exists in the relational store — 100 machines checked
  [PASS] every telemetry sensor_id exists in the relational store — 584 sensors checked
  [PASS] every QC result resolves to a batch
  [PASS] every QC result resolves to a product
  [PASS] every RCA resolves to a deviation
  [PASS] every CAPA resolves to a deviation
  [PASS] every failure resolves to a machine
  [PASS] every batch stage resolves to a batch
  [PASS] every production record resolves to a shift instance
  [PASS] every event resolves to a declared event type
```

### Schema changes against existing data

Telemetry uses the narrow `(ts, machine_id, sensor_id, tag, value, unit, quality)`
shape, so **a new sensor tag never requires DDL in any backend**. Registry
contents — states, event types, equipment classes, sensors — are stored as rows,
so a new state is data rather than a migration. Topology tables are reconciled on
open: missing nullable columns are added in place, and existing data survives.
Every run records a config fingerprint so old and new data stay comparable.

---

## The live feed

`--live` runs indefinitely, paced against wall time, pushing the same messages a
factory gateway would.

```bash
# JSONL to stdout — no broker needed, pipe it anywhere
python -m pharma_sim run --live --speed 60 --sink jsonl | your_consumer

# MQTT
docker compose up -d mosquitto
python scripts/mqtt_consumer.py &
python -m pharma_sim run --live --speed 60 --sink mqtt

# 7 days of history, then keep streaming
python -m pharma_sim run --days 7 --then-live --sink mqtt,jsonl_file
```

Measured against a real broker: **1,340 msg/s from 100 machines across 66
distinct tags**, telemetry and events interleaved.

Telemetry message:

```json
{"kind":"telemetry","timestamp":"2026-01-01T06:14:03","plant_id":"PLANT-01",
 "unit_id":"UNIT-06","machine_id":"TP-006","sensor_id":"TP-006:vibration",
 "tag":"vibration","value":2.31,"unit":"mm/s","quality":"GOOD",
 "state":"RUNNING","run_id":"RUN-0001"}
```

MQTT topics are templated and configurable:
`pharma/{plant_id}/{unit_id}/{machine_id}/telemetry`.

**Operational guarantees.** Each sink runs on its own thread behind a bounded
queue, so a slow or dead sink can never stall the simulation clock. On overflow
the oldest batch is dropped and a counter is incremented, logged and reported by
`status` — never silently. A missing MQTT broker degrades to warn-and-buffer with
a bounded offline buffer and automatic reconnect; it never takes the run down.
`SIGINT` shuts down gracefully and flushes every sink.

---

## Dashboard and API

```bash
uv pip install -e ".[api]"

pharma_sim serve --port 8000                          # browse the stored dataset
pharma_sim serve --live --speed 120 --warmup-hours 6   # host a running plant
```

Then open <http://localhost:8000>.

**Read-only by design.** There is no authentication because there is nothing to
protect: the API has no POST, PUT, PATCH or DELETE route, no form and no
mutation path. A test asserts that by walking the OpenAPI schema, so it stays
true.

### Two modes

- **Historical** — queries whatever the last run produced. Sensor charts read
  their series back from Parquet, ClickHouse or the hypertable, whichever is
  configured.
- **Live** (`--live`) — the API hosts a simulator on a background thread. It
  fast-forwards a warm-up so the dashboard opens onto a plant that is already
  producing, then paces against wall time and pushes telemetry and events over a
  WebSocket. The dashboard registers as an ordinary sink, so the browser sees
  exactly the same messages an MQTT or JSONL consumer does.

  Measured: 100 machines, 66 distinct tags, ~1,000 messages/second with zero
  drops. Each browser gets a bounded queue that drops oldest and *counts it*, so
  a tab left open in the background cannot slow the simulation, and the header
  reports any drops rather than hiding them.

### Views

| View | Shows |
|---|---|
| Plant | KPI tiles, machine-state distribution, production and OEE per shift, OEE by unit, live activity feed |
| Units | Per-unit table with OEE, output, downtime, failures; output and downtime bars |
| Machines | Filterable table; select a machine for its facts, current values with data-quality flags, event timeline, and one live chart per sensor tag |
| Batches | Filterable table; select a batch for stage-by-stage achieved process values, QC results against limits, and the linked deviation → RCA → CAPA |
| Quality | QC failure rates by parameter, deviations, recent results |
| Reliability | Downtime by failure category, concluded root causes, failures, RCA with evidence, CAPA verification, maintenance |
| People | Shift instances with attendance and OEE; the workforce with skills and certifications |
| Events | The full event stream, filterable by category and severity |

Machine and batch selections are in the URL (`#machines/TP-001`), so a view can
be shared or reloaded.

### Charts

Hand-built SVG rather than a charting library, so the marks are exact and the
page has no external dependency — it works offline, and a test asserts there is
no CDN reference. Three rules are enforced in the chart code itself rather than
left to the caller:

- **No dual axes, ever.** Measures of different scale become separate charts. A
  machine's sensors are therefore small multiples — one chart per tag — which is
  also the only honest way to show mm/s beside °C beside amps.
- **At most three series on one plot.** The categorical palette was validated
  with the skill's checker: three slots clear every colourblind-separation and
  normal-vision floor on all pairs in both light and dark mode; the fourth fails
  against the second. So the code refuses a fourth series instead of cycling hues.
- **Specification limits never squash the signal.** A limit line is drawn only if
  it falls within the observed range. An alarm limit far from the data would
  compress the measurement into a flat line, and its absence already tells you
  the value is nowhere near it.

Status colours (good / warning / serious / critical) are reserved for machine
state and QC results, never reused as a series colour, and always paired with an
icon and a text label so colour never carries meaning alone. Dark mode is a
selected set of steps for the dark surface, not an inversion.

---

## CLI reference

```bash
pharma_sim validate                      # lint config; reports every problem at once
pharma_sim schema --output schemas/      # JSON Schema for editor support
pharma_sim init                          # build the factory, persist topology
pharma_sim status                        # counts, clock, sink stats, drops
pharma_sim verify-integrity              # cross-store referential checks

pharma_sim run --days 30                 # fast-forward: no real-time wait
pharma_sim run --hours 24 --speed 10     # paced
pharma_sim run --live --speed 60 --sink jsonl,mqtt
pharma_sim run --days 7 --then-live --sink mqtt
pharma_sim run --days 30 --export data/export

pharma_sim inject-failure --machine TP-006 --failure BEARING_FAILURE
pharma_sim scenario MACHINE_FAILURE
pharma_sim scenario --list

pharma_sim export --output data/export --format both

pharma_sim serve --port 8000              # read-only dashboard over stored data
pharma_sim serve --live --speed 120       # host a running plant and stream it
```

Global flags: `--config DIR`, `--seed N`, `--log-level`, `--json-logs`.

### Failure injection

An injected failure is not a record — it enters the same machinery as a
naturally-arising one and propagates the whole way:

```bash
python -m pharma_sim inject-failure --machine TP-001 --failure BEARING_FAILURE --hours 48
```

reports the precursor trajectory, the warning, the fault, the affected batches,
the RCA conclusion, and whether that conclusion was **correct** against the
hidden ground truth.

### Scenarios

`NORMAL_PRODUCTION`, `MACHINE_FAILURE`, `MULTIPLE_MACHINE_FAILURE`, `QC_FAILURE`,
`OPERATOR_ERROR`, `RAW_MATERIAL_SHORTAGE`, `HVAC_FAILURE`, `POWER_FAILURE`,
`SENSOR_FAILURE`, `HIGH_DEMAND`, `LOW_DEMAND`, `MAINTENANCE_OVERDUE`,
`ENVIRONMENTAL_EXCURSION`. Each is a duration, a set of temporary config
overrides and a list of timed interventions — all declared in `scenarios.yaml`.

---

## Data volumes

Telemetry dominates, and its cadence is the main dial:

| Cadence | 30-day sensor rows | Notes |
|---|---|---|
| 60 s (**default**) | ~25 M | ~17 min to generate, ~4 bytes/row in ClickHouse |
| 10 s | ~150 M | expect GBs and a long run |
| 1 / 5 s | live only | for the streaming feed |

Business data is deliberately coarse: production records aggregate per machine
per shift (~9,000 rows over 30 days) rather than per minute, and events are
event-driven (~65,000). This is the §12 requirement not to generate
high-frequency data for business events.

Change it in `config/plant.yaml` (`sensor_sample_interval_s`) or per run with
`--sensor-interval-s` on `scripts/generate_dataset.py`.

---

## Datasets and their columns

`pharma_sim export` writes three separate places:

```
data/export/
  reference/      plants, units, equipment_classes, machines, sensors, plc_tags,
                  employees, products, shifts, states, event_types, runs
  operational/    production, machine_events, machine_failures, employee_events,
                  shift_data, batch_data, batch_stages, qc_results, maintenance,
                  deviations, rca, rca_evidence, capa, machine_state_history,
                  oee, events
  manifest.json   row counts, and where the other two stores are
data/telemetry/   sensor_readings, partitioned Parquet (not duplicated on export)
data/eval/        ground_truth_events, prediction_labels
```

### The information boundary

This is what makes the dataset usable for evaluating a diagnostic system.

**Operational data** records what a plant would actually know at the time. The
`failures` table carries `category`, `severity`, `symptom`, alarms, downtime and
affected batches — but deliberately **not** the failure mode or its root cause.
An `rca` row is a *conclusion* drawn by the investigation, and it can be wrong.

**Evaluation data** records what the simulator knows: `ground_truth_events` holds
the true failure mode, the true root cause, onset, scheduled fault instant,
whether it was averted, and which batches and QC failures it caused.

`prediction_labels` gives forward-looking targets over every precursor window:
`rul_hours` (exact), `will_fail_24h` / `_72h` / `_168h`, `failure_mode`,
`root_cause`, `degradation_stage`, and `averted`.

The two are written to different stores, the export puts them in different
directories, and a test asserts no operational table exposes a label column.
**Joining them into training features would leak the answer.**

The RCA engine scores around 70–80% against ground truth on the shipped
configuration, with most of its misses being an honest `UNDETERMINED` rather than
a wrong answer. It is *supposed* to be fallible — a diagnostic engine that is
right by construction tells you nothing about whether yours works.

### Questions the dataset can answer

Why did a machine fail; what happened before it; which sensor moved first; which
batches were affected; who was operating it; how much production was lost;
whether QC passed; what the root cause was; what action was taken; whether that
action worked (CAPA verification batches); which machines are likely to fail next
(`prediction_labels`); which unit has the worst OEE (`oee_snapshots`); which
failure mode causes the most downtime; and which process parameters correlate
with QC failures (`batch_stages.parameters` joined to `qc_results`).

`scripts/trace_batch.py` walks one batch's whole genealogy from the stored data
alone, and `--failure` walks the same relationships in reverse.

---

## Modelling notes and limitations

Stated plainly, because a synthetic dataset is only as useful as your
understanding of what it does and does not model.

- **Degradation progresses in calendar time once initiated.** Hazards are only
  *evaluated* on machines that are not down or offline, so wear accrues with
  operating exposure — but after a mode initiates, its progress to the scheduled
  fault runs on wall-clock time. Fixing the fault instant at onset is what makes
  exact RUL labels possible; the trade is that a machine idle for days still
  advances toward its fault.
- **One machine per batch stage.** A stage occupies a single machine rather than
  a line of machines in parallel.
- **Alarm limits are only evaluated in productive states**, and scale with the
  active product setpoint. A stopped machine reading zero speed is not an alarm,
  and a 640 mg tablet is not a high-weight alarm against a 550 mg limit.
- **Stage duration** is either a configured process time (`stage_duration_min`)
  or derived from batch size and throughput, depending on the equipment. Dryers
  run for their drying time; presses take as long as the batch divided by rate.
- **OEE excludes unscheduled time.** Availability is measured over loading time —
  running, broken, or idle *while holding work*. A machine with nothing assigned
  is not "unavailable", so that time is excluded and reported separately as
  utilisation. Conflating the two is what makes plant OEE look catastrophic when
  the real story is light loading; both numbers are on the dashboard.
- **Machines have a declared `duty`, not just a route.** `_select_machine` only
  hands a batch stage to equipment whose sensor tags measure that stage's process
  parameters — correct, since running compression on a deduster would produce a
  stage with no real process values. Left there, every utility and inline
  support machine (HVAC, purified water, compressed air, dedusters, metal
  detectors, cartoners, labellers) would sit `unscheduled` forever, which is not
  how a real plant runs them. `machines.yaml` declares three duty classes:
  `batch` (the default — takes stage assignments), `continuous` (a utility that
  runs unattended around the clock and stops only for failure or maintenance),
  and `coupled` (inline support that follows whichever line it sits on). The
  duty manager (`engine/duty_manager.py`) drives `continuous`/`coupled` machines
  to the plant's productive state or back to idle every production tick, reading
  only state roles and the `duty` field — no equipment names. The shipped layout
  was also rebalanced against measured per-stage demand (packaging was the
  bottleneck at 3 of 10 machines batch-eligible; it is now 10 of 10), which is
  what moved plant utilisation from 24.8% to ~58%.
- **`hazard_scale` is a tuning knob.** The shipped value targets ~30 degradation
  episodes per 30 days across 100 machines. It was calibrated by measuring the
  plant-wide hazard budget, not guessed.
- **RCA is given the failure's category** — a real maintenance report states the
  observed symptom class — but never the mode or the root cause.
- Sensor baselines, noise, limits and failure parameters are plausible rather
  than sourced from specific equipment. Calibrate them for your own use case;
  that is what the config files are for.

---

## Project layout

```
pharma_factory_simulator/
├── config/                       manufacturing YAML, plus lab/, clinical/,
│                                 lifecycle/ and examples/minimal_factory/
├── src/pharma_sim/
│   ├── config/                   Pydantic models, loader, linter, fingerprint, drivers
│   ├── registry/                 states, event types, equipment, failures, QC, topology
│   ├── engine/                   clock, scheduler, event bus, ids, rng, context,
│   │                             telemetry sampler, shift manager, batch manager,
│   │                             scenario engine
│   ├── domain/                   plant, machine, sensor, plc, employee, shift, batch,
│   │                             qc, oee, environment, history, failure_engine,
│   │                             maintenance, quality_management, ground_truth
│   ├── lab/                      chromatography, method model, ICH Q2 validation,
│   │                             ICH Q1A stability and shelf-life fit
│   ├── clinical/                 RECIST derivation, lesion model, survival, CTMS,
│   │                             EDC, oversight, study assembly
│   ├── lifecycle/                the spine: batch to lot to kit to dose, and
│   │                             the checks that walk it
│   ├── storage/                  schema, protocols, facade, factory, sqlite, postgres,
│   │                             clickhouse, timescale, parquet
│   ├── streaming/                base, router, jsonl_sink, mqtt_sink
│   ├── exports/                  exporter
│   ├── simulator.py              assembly, lifecycle, run loop
│   └── __main__.py               CLI
│   ├── api/                      FastAPI app, read-only service layer, live hub
│   │   └── static/               dashboard: index.html, app.js, charts.js, styles.css
├── scripts/                      generate_dataset, generate_lab_dataset,
│                                 generate_clinical_dataset, verify_realism,
│                                 phase0_digest,
│                                 initialize_factory, mqtt_consumer, trace_batch
├── tests/                        592 tests
├── docker/mosquitto.conf
├── Dockerfile
└── docker-compose.yml            postgres+timescale, clickhouse, mosquitto
```

`storage/`, `generators/`, `exports/` and `api/` appear inside the package rather
than at the repository root as the original brief sketched — that keeps them
importable and avoids top-level names as generic as `storage` colliding with
other packages.

---

## Testing

```bash
.venv/bin/python -m pytest -q                          # Docker-free, ~4 minutes
.venv/bin/python -m pytest -q -m "postgres or clickhouse"   # needs containers
```

592 tests run with no infrastructure, including the API and the live WebSocket;
13 more cover PostgreSQL, TimescaleDB and ClickHouse and are skipped
automatically when those services are unreachable.

Two of them are worth knowing about because they defend properties the type
system cannot:

* `test_determinism.py` shells out with `PYTHONHASHSEED` set to three different
  values. In-process tests structurally cannot catch hash-order dependence — the
  seed is fixed for the life of an interpreter — and a single `tuple({...})` in
  the factory builder was once enough to make every run irreproducible.
* `test_lifecycle_spine.py` has a test per spine check confirming it fails
  against a graph broken in that one way, so a check cannot quietly become
  vacuous.

Counts are configuration, so the suite asserts against *loaded config* rather
than literals — a config edit must not break the tests. A separate test checks
that the shipped default configuration produces the brief's numbers.

The tests worth knowing about:

- **Causality** — precursors rise together and before the warning; the warning
  precedes the fault; force → hardness → disintegration → dissolution is
  monotonic; nominal inputs land on target; QC never fails independently of
  process conditions.
- **Labels** — RUL decreases monotonically to zero at the fault; horizon flags
  agree with remaining life; averted episodes claim no failure.
- **Isolation** — no operational table exposes a label column; RCA is *not*
  always right (if it were, it could see the answer).
- **Schema-agnosticism** — the alternate factory runs unchanged; adding a state or
  a sensor tag in YAML needs no code change and no DDL.
- **Integrity** — foreign keys genuinely reject orphans; every state transition in
  the dataset is legal under the configured table; the cross-store check catches
  an injected orphan.
- **Reproducibility** — same seed, identical event-stream digest; different seed,
  different.
- **Storage parity** — one seeded run means the same thing in SQLite+Parquet and
  in Postgres+ClickHouse.
- **Streaming** — bounded-queue drops are counted; a dead sink does not stall the
  others; a missing broker buffers instead of raising; MQTT is tested against a
  fake client so no broker is needed.
- **API** — no mutating route exists (asserted against the OpenAPI schema); no
  operational endpoint serves a label or ground-truth field; the dashboard has no
  external asset references; the live WebSocket delivers telemetry frames.
