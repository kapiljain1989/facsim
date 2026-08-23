# Configuring a factory

This is the file you want if you're trying to build a **new** factory —
different equipment, a different product, a different plant entirely — rather
than just reading how the shipped one works. The README's
[Configuration](../README.md#configuration) section is the reference table;
this is the walkthrough.

Everything the simulator knows about a factory lives in fifteen YAML files
under `config/`. There is no Python to edit. If you find yourself wanting to
add an `if equipment_class == "..."` somewhere, that's the signal you're
fighting the design — the answer is almost always "declare it in YAML instead."

---

## Contents

- [The mental model](#the-mental-model)
- [The dependency graph — what to write first](#the-dependency-graph--what-to-write-first)
- [File by file](#file-by-file)
- [Validating as you go](#validating-as-you-go)
- [Worked example: a two-machine syrup line](#worked-example-a-two-machine-syrup-line)
- [Gotchas that aren't obvious from the schema](#gotchas-that-arent-obvious-from-the-schema)
- [Tuning failure frequency](#tuning-failure-frequency)
- [Checklist for a new factory](#checklist-for-a-new-factory)

---

## The mental model

Three ideas make everything else fall into place:

1. **Nothing is hard-coded, so everything is a reference.** `machines.yaml` says
   a tablet press is in `UNIT-06`; that only works if `UNIT-06` exists in
   `units.yaml`. `qc_rules.yaml` reads `main_compression_force` as a transfer
   input; that only works if some product's `process_parameters` declares that
   parameter for the stage the QC test belongs to. Almost every field in every
   file is either an **id you're inventing** or a **reference to an id declared
   elsewhere**. Get the references right and the engine does the rest.

2. **States and events have *meaning*, not just names.** The engine never
   checks `if state == "RUNNING"`. It checks `states.is_productive(state)`,
   and `states.yaml` decides which state(s) that role points at. This is why
   you can delete `CLEANING`, add `QUALIFICATION`, or rename everything in
   Chinese, and the OEE math still works — as long as every state you keep is
   assigned to the roles the engine actually reads (see
   [states.yaml](#statesyaml)).

3. **Quality and failure are consequences, computed forward from process
   conditions — never sampled independently.** You don't configure "QC fails
   8% of the time." You configure a transfer function
   (`hardness = f(compression_force, moisture)`), and QC fails when the
   *process* drifts far enough, because a failing bearing shifted the
   compression force, because lubrication was overdue. If you're tempted to
   hard-code a failure rate or a reject rate, look for the upstream driver
   instead — that's where it belongs.

---

## The dependency graph — what to write first

The linter (`pharma_sim validate`) checks files in this order, which is also
the order you should *write* them in — each step's ids become the vocabulary
the next step references:

```
states.yaml ─────────┐
event_types.yaml ─────┤
                      ├──▶ units.yaml ──▶ machines.yaml ──▶ sensors.yaml
                      │         (sensor profiles machines.yaml inherits)
                      ▼
                 products.yaml ──▶ qc_rules.yaml
                      │
                      ▼
                 failures.yaml ──▶ rca_rules.yaml
                      │
                      ▼
        deviations.yaml, scenarios.yaml, sinks.yaml
                      │
                      ▼
                storage.yaml, plant.yaml, shifts.yaml, maintenance.yaml
                (mostly standalone — few cross-references in, from everywhere)
```

In practice: start from `config/examples/minimal_factory/` (a complete,
working, *much smaller* factory — 2 units, 5 machines, its own 5-state model)
and edit outward from there, rather than starting from the 100-machine default
and hoping to cut it down. Copy the directory, then work through the sections
below in order.

```bash
cp -r config/examples/minimal_factory config/my_factory
.venv/bin/python -m pharma_sim --config config/my_factory validate
```

Run `validate` after every file you touch. It is fast, it is precise (file,
YAML path, and a fix hint per issue), and it will catch a dangling reference
immediately instead of at hour 300 of a 30-day run.

---

## File by file

### `plant.yaml`

Identity and pacing — the only file with no ids other referenced files point
at.

```yaml
plant_id: PLANT-01
name: My New Factory
location: Somewhere
timezone: Asia/Kolkata
plant_manager_name: A. Manager

simulation:
  seed: 42                                    # reproducibility: same seed -> byte-identical run
  start_time: 2026-01-01T06:00:00
  speed_sim_minutes_per_real_second: 60.0     # pacing for --live; ignored by fast-forward
  sensor_sample_interval_s: 60.0              # historical backfill cadence
  live_sensor_sample_interval_s: 5.0          # live-feed cadence — usually denser
  hazard_evaluation_interval_min: 60.0        # how often the failure hazard is evaluated
  production_tick_min: 5.0                    # how often machines accrue time/production
  label_interval_min: 30.0                    # forward-looking-label emission cadence
  rca_lookback_hours: 72.0                    # evidence window RCA reads
  rca_investigation_delay_hours: 4.0          # RCA lands after the fault, like a real one would

ambient:                                      # plant-wide latent driver — see "sensors.yaml"
  temperature_c: 22.0
  temperature_diurnal_amplitude_c: 2.5
  humidity_pct: 45.0
  humidity_diurnal_amplitude_pct: 5.0
  excursion_probability_per_day: 0.02         # rare HVAC/ambient upsets
  excursion_temperature_delta_c: 6.0
  excursion_duration_hours: 3.0
```

`sensor_sample_interval_s` is the biggest lever on data volume: 60s over 30
days across 100 machines is already ~25M rows. Check the README's volume table
before lowering it.

### `states.yaml`

The machine state model and, critically, the **roles**. This is the file that
makes the rest of the engine equipment-agnostic — read it slowly.

```yaml
states:
  - id: IDLE
    description: Available and powered, waiting for work.
    production_rate_factor: 0.0     # fraction of nominal output while in this state
    energy_factor: 0.15             # fraction of nominal power draw
  - id: RUNNING
    production_rate_factor: 1.0
    energy_factor: 1.0
  # ... every other state you need

transitions:
  IDLE: [STARTING, MAINTENANCE, OFFLINE]   # legal next states — anything absent is rejected
  STARTING: [RUNNING, FAULT, IDLE]
  # ...

roles:
  initial: OFFLINE                  # what a freshly-built machine starts in
  productive: [RUNNING, WARNING]    # counts toward produced quantity and OEE availability
  downtime: [FAULT, MAINTENANCE]    # machine unavailable
  idle: [IDLE]
  requires_operator: [STARTING, RUNNING, WARNING]   # shift staffing cares about this set
  planned_stop: [CLEANING, CHANGEOVER, OFFLINE]     # excluded from OEE loading time
```

Every role the engine reads must be assigned to *something* — `productive` and
`initial` are mandatory; others (`cleaning`, `changeover`, `warning`) are
optional, and the engine skips the corresponding step if you don't have one
(the minimal example has no `CLEANING` state at all).

`force_route_to()` walks the `transitions` table to get a machine from
wherever it is to wherever a role points — e.g. `IDLE → STARTING → RUNNING` if
there's no direct edge. You never need a direct edge for every pair; a path
just needs to *exist*.

### `event_types.yaml`

The event vocabulary. Every event the engine publishes is checked against this
list at startup — an event type the code emits but this file doesn't declare
fails immediately, not silently. You will rarely need to touch this file for a
new *factory* (only for new *behaviour*), but every entry needs:

```yaml
severities: [INFO, MINOR, MAJOR, CRITICAL]

event_types:
  - id: MACHINE_FAILURE
    category: MACHINE
    default_severity: MAJOR
    required_fields: [failure_id, failure_mode, category]
```

### `units.yaml`

Production units — the coarse-grained "departments" that own process stages
and staffing.

```yaml
worker_roles: [OPERATOR, SENIOR_OPERATOR]
manager_role: UNIT_MANAGER
technician_role: TECHNICIAN
qc_analyst_role: QC_ANALYST
skill_levels: [JUNIOR, INTERMEDIATE, SENIOR]

units:
  - id: UNIT-01
    name: Mixing
    sequence: 1                     # process order, for display/traceability
    process_stage: MIXING           # the token products.yaml routes reference
    worker_count: 4
    environment_sensitivity: 1.0    # how strongly ambient excursions affect this unit's sensors
```

`process_stage` is the join key to `products.yaml`'s `manufacturing_process`
list and to `machines.yaml`'s layout — get this string right and consistent.

### `machines.yaml`

Equipment classes (the *kind* of machine) plus the plant `layout` (how many of
each kind, in which unit).

```yaml
equipment_classes:
  - id: syrup_mixer
    name: Syrup Mixer
    duty: batch                      # batch | continuous | coupled — see below
    sensor_profile: mixing_tank      # optional: inherit a profiles.yaml block
    sensors:
      - {tag: rpm, override: {sigma: 0.4}}     # patch an inherited tag
      - {tag: legacy_tag, remove: true}        # drop an inherited tag
      - {tag: fill_level, baseline: 80, unit: '%', rate_s: 30}   # or add one inline
    nominal_rate_per_hour: 500.0
    stage_duration_min: 45.0         # fixed process time; omit to derive from batch size / rate
    pm_interval_hours: 720.0
    pm_duration_hours: 4.0
    base_reject_rate: 0.004
    setup_duration_min: 20.0
    cleaning_duration_min: 30.0
    changeover_duration_min: 45.0
    startup_duration_min: 5.0
    failure_modes: []                # empty = every applicable mode in failures.yaml can occur

layout:
  UNIT-01:
    - {equipment_class: syrup_mixer, count: 2, id_prefix: MX,
       commissioned_from: 2019-01-01, commissioned_to: 2024-06-30}
```

**`duty` decides what "having work" means, and it matters more than it looks.**
`_select_machine` only ever hands a batch stage to equipment whose sensor tags
actually measure that stage's declared process parameters — correctly, since
running a compression stage on a deduster would produce a stage with no real
readings. But that means anything that *isn't* measured process equipment
needs an explicit duty, or it sits unscheduled forever:

- `batch` (default) — takes a stage when routed one; idle otherwise.
- `continuous` — a utility (HVAC, purified water, compressed air) that runs
  unattended around the clock and stops only for failure or maintenance.
- `coupled` — inline support (deduster, metal detector, cartoner) that follows
  whichever line it sits on: running when a batch-duty machine in its unit is
  producing, idle when the unit goes quiet.

If your new factory has *any* equipment that isn't itself a measured
process step — dust extraction, water systems, inspection cameras, anything
downstream of the "real" machine — give it `duty: continuous` or
`duty: coupled`, or it will never appear as productive.

### `sensors.yaml`

Reusable sensor **profiles**, each a list of tags. A profile is optional —
`machines.yaml` may inherit one, patch it, or skip profiles and declare tags
inline.

```yaml
default_rate_s: 60.0

profiles:
  mixing_tank:
    - tag: rpm
      unit: rpm
      baseline: 120.0
      sigma: 1.5              # measurement-noise std dev
      rho: 0.90                # AR(1) autocorrelation — how strongly a sample remembers the last
      drift_per_day: 0.02      # slow bounded random walk
      drift_limit: 2.0
      health_sensitivity: 0.05 # generic wear response as a fraction of baseline at full degradation
      hard_min: 0.0
      hard_max: 200.0
      warn_low: 90.0
      warn_high: 150.0
      alarm_low: 70.0
      alarm_high: 170.0
      process_parameter: true  # this tag is what a QC transfer function reads
      state_factors:
        offline: {mult: 0.0, sigma_mult: 0.0}
        idle: {mult: 0.0, sigma_mult: 0.0}
        starting: {mult: 0.6, sigma_mult: 3.0}   # unstable during ramp-up
        warning: {mult: 1.05, sigma_mult: 2.0}   # elevated once degrading
```

Every reading is `baseline × state_factor + AR(1) fluctuation + noise + drift +
health/failure coupling + ambient coupling`, never a plain random draw. A tag
with no `state_factors` entry for a given state just uses its baseline
unmodified in that state — declare an entry only where behaviour actually
differs.

**Alarm limits are only evaluated in productive states**, and `warn_*`/`alarm_*`
should bracket the *achievable* range, not the physical range — a stopped
machine reading zero is not an alarm.

### `shifts.yaml`

```yaml
absenteeism_rate: 0.04
overtime_probability: 0.08
overtime_duration_min: 60.0
clock_in_jitter_min: 8.0
clock_out_jitter_min: 6.0

shifts:
  - code: A
    name: Morning
    start: "06:00"
    end: "14:00"
    breaks:
      - {label: LUNCH, start: "11:00", duration_min: 30}
```

A shift whose `end` is earlier than its `start` (like a night shift, `22:00`
→ `06:00`) is handled correctly — the scheduler doesn't assume `start < end`.

### `products.yaml`

The product catalogue: routes, setpoints, and which QC tests apply.

```yaml
max_concurrent_batches: 12   # work-in-process cap — see the WIP section below

products:
  - product_id: SYRUP-A
    product_name: Cough Syrup A
    dosage_form: LIQUID
    batch_size: 5000
    target_quantity: 5000
    demand_weight: 1.0        # relative order frequency vs other products
    manufacturing_process: [MIXING, BOTTLING]   # must all be declared units.yaml process_stages
    raw_materials:
      - {material_id: RM-SUGAR, name: Sugar Syrup Base, quantity_kg: 200.0, variability: 0.02}
    process_parameters:
      MIXING:
        rpm: {target: 120.0, min: 90.0, max: 150.0, unit: rpm}
    qc_specifications: [viscosity_qc]   # must exist in qc_rules.yaml
```

`process_parameters` is the other half of the sensor-tag contract: a stage's
parameter names here are what `_select_machine` matches against equipment
sensor tags, and what QC transfer functions read as inputs. **The names must
match exactly** — `rpm` here, `rpm` as a sensor tag in `sensors.yaml`.

### `qc_rules.yaml`

QC parameters and the transfer functions that make a result a *consequence* of
process conditions.

```yaml
results: [PASS, FAIL, OOS, OOT]
reject_on_final_failure: true

parameters:
  - id: viscosity_qc
    name: Viscosity
    stage: MIXING              # must be a manufacturing_process stage
    phase: IN_PROCESS           # or FINAL
    unit: cP
    target: 450.0
    lower_limit: 380.0
    upper_limit: 520.0
    noise_sigma: 8.0
    health_sensitivity: 0.10   # how much a degrading machine pushes this off-target
    sample_size: 3
    transfer:
      intercept: 100.0          # chosen so nominal inputs land exactly on target
      terms:
        - {input: rpm, coef: 2.9}   # process_parameter, another QC id, or an engine driver
      clip_min: 50.0
```

`value = intercept + Σ(coef × input^power)`. Inputs can be a process
parameter from `products.yaml`, another QC parameter (evaluated in dependency
order, so chains like `moisture → hardness → disintegration` work), or an
engine driver like `machine_health`. **Pick the intercept so nominal inputs
land on target** — that's what makes a shifted setpoint or a degrading machine
move the result in a legible direction instead of an arbitrary one.

### `failures.yaml`

The hazard model and precursor signatures — where random-but-rare failure
comes from.

```yaml
hazard_scale: 0.33          # global tuning knob for overall frequency — see below
max_concurrent_modes: 2

hazard_factors:             # each is intercept + Σ(coef × driver), clipped if declared
  age: {intercept: 1.0, terms: [{input: age_years, coef: 0.07}]}
  operating_hours: {intercept: 1.0, terms: [{input: operating_khours, coef: 0.06}]}
  maintenance_debt: {intercept: 1.0, terms: [{input: pm_overdue_ratio, coef: 0.9}], clip_max: 4.0}
  load: {intercept: 0.55, terms: [{input: load_factor, coef: 0.45}]}
  environment: {intercept: 0.85, terms: [{input: environment_stress, coef: 0.35}]}
  operator: {intercept: 0.95, terms: [{input: operator_inexperience, coef: 0.30}]}

failure_modes:
  - id: BEARING_FAILURE
    category: MECHANICAL
    sensor_profiles: [mixing_tank]      # applies to classes using this profile...
    equipment_classes: []               # ...or name classes directly; both empty = every machine
    mtbf_operating_hours: 3800.0
    weibull_beta: 2.4                   # > 1: wear-out; == 1: memoryless (e.g. grid interruption)
    incubation_hours_min: 24.0
    incubation_hours_max: 168.0         # cap around 168h or warnings land past a typical run
    severity: MAJOR
    root_cause: INSUFFICIENT_LUBRICATION
    root_cause_description: Lubrication interval exceeded.
    warning_threshold: 0.38             # ~70% of incubation is a reasonable rule of thumb
    precursors:
      - {tag: vibration, delta_fraction: 1.10, curve: exponential, sigma_growth: 0.65}
    effects:
      production_rate_factor: 0.0
      reject_rate_add: 0.055
      process_variability_gain: 0.60
      process_parameter_shifts: {rpm: 15.0}
    repair:
      duration_hours: 6.0
      cost: 1250.0
      parts: [bearing set]
      technicians: 2
```

```
λ(t) = (β/mtbf) × (h/mtbf)^(β−1) × age × operating_hours × maintenance_debt × load × environment × operator
P(initiate in Δt) = 1 − exp(−λ·Δt)
```

Onset schedules a fixed fault instant (via the sampled incubation duration) —
that's what makes exact remaining-useful-life labels possible, and what makes
maintenance genuinely able to **avert** a failure rather than merely delay an
inevitability. Every `precursors` tag must be a real sensor tag on the
equipment the mode applies to (the linter checks this).

### `maintenance.yaml`

The feedback loop: deferred PM raises `maintenance_debt`, which raises hazard,
which makes failure more likely — and repair resets it.

```yaml
types: [PREVENTIVE, CORRECTIVE, EMERGENCY, PREDICTIVE]
technician_pool: 8
pm_lead_time_hours: 24.0
pm_deferral_probability: 0.18
pm_deferral_hours: 72.0
predictive_response_probability: 0.45   # chance a WARNING state triggers action before FAULT
predictive_response_delay_hours: 8.0
corrective_delay_hours: 1.0
hourly_labour_cost: 60.0
corrective_effectiveness: 1.0
```

### `rca_rules.yaml`

Evidence rules (statistical tests over the pre-fault sensor window) and
root-cause rules (which evidence combinations imply which cause, with a 5-Why
chain). This engine is deliberately **fallible** — it only sees the failure's
*category*, never the true mode or root cause, which live in the isolated
ground-truth store so RCA accuracy is something you can actually measure.

```yaml
fallback_root_cause: UNDETERMINED
verification_batches: 3

evidence_rules:
  # delta: mean(second half) vs mean(first half), as a fraction. Negative
  # min_delta_fraction matches a DECLINE — a fall is evidence too.
  - {id: VIB_LARGE_RISE, tag: vibration, min_delta_fraction: 0.60, weight: 1.6}
  # statistic: variance_ratio instead of delta, for "signal got erratic" evidence
  - {id: VIB_ERRATIC, tag: vibration, statistic: variance_ratio, min_delta_fraction: 2.2, weight: 1.5}
  # non-sensor evidence, keyed by `signal` instead of `tag`
  - {id: PM_OVERDUE, signal: pm_overdue_hours, min_value: 24.0, weight: 1.3}

rules:
  - id: RCA-LUBRICATION
    root_cause: INSUFFICIENT_LUBRICATION
    categories: [MECHANICAL]
    evidence: [VIB_LARGE_RISE, PM_OVERDUE]
    min_score: 2.6                       # sum of matched evidence weights must clear this
    fishbone_category: MACHINE
    five_why:
      - Why did the machine stop? A bearing fault tripped the drive.
      - Why did the bearing fault? Vibration and temperature rose progressively.
      - Why did they rise? The bearing ran with inadequate lubricant film.
      - Why was lubrication inadequate? The scheduled task was not performed on time.
      - Why was it not performed? Preventive maintenance was deferred past its interval.
    corrective_action: Replace the bearing set and verify vibration returns to baseline.
    preventive_action: Add vibration trending with an alert at 1.5x baseline.
```

Overlapping evidence between rules (two root causes both matching on a
vibration rise) is intentional — a diagnostic system that's right by
construction can't be evaluated against ground truth.

### `deviations.yaml`

Which events open a quality deviation, and whether it needs RCA/CAPA.

```yaml
statuses: [OPEN, INVESTIGATION, CAPA_PENDING, CLOSED]

rules:
  - id: DEV-RULE-MACHINE-FAILURE
    trigger_event: MACHINE_FAILURE   # must be a declared event_types.yaml id
    severity: MAJOR
    title: Unplanned equipment failure during production
    requires_rca: true
    requires_capa: true
```

### `scenarios.yaml`

Scripted, timed interventions for demos and targeted testing — injected
failures enter the *same* degradation machinery as naturally-arising ones, so
precursors, warnings, QC impact, deviation, RCA and CAPA all still follow.

```yaml
scenarios:
  - id: MACHINE_FAILURE
    description: A mixer develops a bearing fault mid-shift.
    duration_hours: 48.0
    actions:
      - {type: inject_failure, at_hours: 3.0, equipment_class: syrup_mixer,
         failure_mode: BEARING_FAILURE, severity: MAJOR}
```

Other action types: `operator_error`, `material_shortage`, `power_interruption`.
`overrides` can temporarily patch any config value by dotted path
(`shifts.absenteeism_rate: 0.12`) for the scenario's duration.

### `sinks.yaml`

Streaming outputs for the continuous feed — disabled by default so a plain
`run` doesn't try to reach a broker.

```yaml
sinks:
  - name: mqtt
    type: mqtt
    enabled: false
    queue_size: 100000
    batch_size: 500
    flush_interval_s: 0.5
    mqtt:
      host: localhost
      port: 1883
      telemetry_topic: "pharma/{plant_id}/{unit_id}/{machine_id}/telemetry"
      offline_buffer: 50000
```

Each sink runs on its own thread behind a bounded queue — a slow or dead sink
drops its oldest batch (counted, logged, visible in `status`) rather than
stalling the simulation clock.

### `storage.yaml`

Which backend holds which data shape. The defaults (`sqlite` + `parquet`) need
no Docker. Switch to `postgres` + `clickhouse` for the production profile
(commented block at the bottom of the file) without touching anything else.

---

## Validating as you go

```bash
.venv/bin/python -m pharma_sim --config config/my_factory validate
```

The linter checks, in dependency order: duplicate ids, dangling state/role
references, undeclared event types, unit/stage references, sensor tag
existence (including every failure-mode precursor and every RCA evidence tag),
QC transfer inputs, deviation trigger events, and sink configuration. Every
issue names its file, its YAML path, and a fix hint.

```bash
.venv/bin/python -m pharma_sim --config config/my_factory schema --output schemas/
```

Emits JSON Schema for every file, for editor autocomplete/validation.

Once `validate` is clean:

```bash
.venv/bin/python -m pharma_sim --config config/my_factory init
.venv/bin/python -m pharma_sim --config config/my_factory status
.venv/bin/python -m pharma_sim --config config/my_factory run --hours 24
```

`status` after a short run is the fastest sanity check — look at the machine
state distribution and make sure things are actually running, not stuck idle.

---

## Worked example: a two-machine syrup line

`config/examples/minimal_factory/` is exactly this, already working — a
2-unit liquids plant (mixing, bottling), 5 machines, its own 5-state model
(no `CLEANING`/`STARTING`), different tags, different shifts. Diff it against
the default `config/` to see the minimum a factory actually needs versus what
the 100-machine default adds on top (33 equipment classes, 3 shifts, a 9-state
model, RCA rules, scenarios).

```bash
diff -rq config/examples/minimal_factory config
.venv/bin/python -m pharma_sim --config config/examples/minimal_factory run --hours 24
```

To build your own from that starting point: rename the ids in `units.yaml`
and `machines.yaml`, redraw `products.yaml`'s routes to match your new
`process_stage`s, adjust `qc_rules.yaml` transfer inputs to match your new
process parameter names, and only then decide whether you need `failures.yaml`
entries beyond the copied defaults. Run `validate` after each file.

---

## Gotchas that aren't obvious from the schema

These came up while calibrating the shipped 100-machine config — they're easy
to reintroduce in a new one.

- **Every stage needs enough *eligible* machines, not just enough machines.**
  `_select_machine` only routes a stage to equipment whose sensor tags include
  that stage's declared process parameters. If a unit has 10 machines but only
  3 of them declare the right tags (the other 7 being support equipment with
  `duty: batch` left as the default), that unit's real capacity is 3, not 10 —
  and it'll become your bottleneck no matter how high you set
  `max_concurrent_batches`. Give support equipment `duty: continuous` or
  `duty: coupled` (see `machines.yaml` above) so it isn't miscounted as idle
  batch capacity.
- **`max_concurrent_batches` is a plant-wide WIP cap, not a per-unit one.**
  Since one batch occupies exactly one machine at a time, this number caps how
  many machines across the *entire* plant can be productive simultaneously.
  Size it against your bottleneck stage's actual throughput
  (`eligible_machines × 60min / stage_duration_min`, adjusted for what fraction
  of products visit that stage), not against total machine count.
- **Balance machine counts against demand-weighted load, not evenly.** A stage
  every product visits (e.g. packaging) needs more machines than one only 20%
  of products touch — an even split (10 machines per unit, say) will starve
  the popular stages and idle the rare ones.
- **Alarm/warn limits need headroom above the achievable range**, not the
  physical range. They're only evaluated in productive states, so a limit set
  right at the setpoint edge will trip on ordinary process noise; leave real
  margin between `target` and `warn_*`.
- **Cap `incubation_hours_max` to something shorter than your typical run
  length.** A precursor window that can run to 336 hours will, for a lot of
  seeds, still be incubating when a 7-day run ends — meaning zero observable
  warnings in your dataset. `warning_threshold` around 0.35–0.40 (of incubation
  elapsed) is a reasonable default.
- **Sensor tag names are load-bearing strings, matched exactly** between
  `sensors.yaml` (or inline `machines.yaml` bindings), `products.yaml`
  `process_parameters`, `failures.yaml` `precursors`, and `rca_rules.yaml`
  evidence — there's no fuzzy matching. The linter catches a dangling
  reference but not a typo that happens to also be a real tag elsewhere.
- **QC transfer intercepts should be solved for, not guessed.** Set every
  input to its nominal/target value, solve `intercept = target − Σ(coef ×
  nominal_input)`, and only then adjust coefficients for the shape of the
  response you want. Guessing an intercept and eyeballing the result tends to
  land QC either always-passing or always-failing.

---

## Tuning failure frequency

`hazard_scale` in `failures.yaml` is the single global knob. To hit a target
rate (the shipped config aims for ~20–40 hard faults per 30 days across 100
machines):

1. Run a multi-day fast-forward at your current scale and count faults in
   `status` or the `failures` export.
2. `hazard_scale_new = hazard_scale_old × (target_faults / observed_faults)`,
   since the hazard is (to first order) linear in the scale.
3. Re-run and recheck — the relationship isn't perfectly linear once
   `maintenance_debt`/`load` factors start clipping, so one or two more passes
   usually gets you within range.

Per-mode `mtbf_operating_hours` and `weibull_beta` let you differentiate
failure rates by mode without touching the global knob — raise MTBF for modes
you want rarer, lower `weibull_beta` toward 1.0 for a mode that should be
closer to memoryless (e.g. an external power interruption) rather than
wear-driven.

---

## Checklist for a new factory

- [ ] Copy `config/examples/minimal_factory/` as a starting point
- [ ] `states.yaml`: states, transitions, and every role the engine reads
      (`initial`, `productive` are mandatory)
- [ ] `units.yaml`: units and their `process_stage` tokens
- [ ] `machines.yaml`: equipment classes with a `duty` set correctly on
      anything that isn't measured process equipment, and a layout that
      balances machine counts against demand
- [ ] `sensors.yaml` / inline bindings: tags with real `state_factors` for
      the states where behaviour actually changes
- [ ] `products.yaml`: routes matching declared `process_stage`s, process
      parameters matching sensor tag names exactly
- [ ] `qc_rules.yaml`: transfer functions with intercepts solved for nominal
      inputs landing on target
- [ ] `failures.yaml`: hazard factors, and per-mode incubation capped well
      inside your intended run length
- [ ] `rca_rules.yaml`: evidence keyed on real tags, root-cause rules with
      enough evidence overlap to be genuinely fallible
- [ ] `deviations.yaml`, `scenarios.yaml`, `sinks.yaml`, `storage.yaml`,
      `maintenance.yaml`, `shifts.yaml`: mostly fine to start from the copied
      defaults
- [ ] `pharma_sim validate` clean
- [ ] A short `run --hours 24` followed by `status` — sane machine-state
      distribution, at least some completed batches, no zero-everywhere
      counters
