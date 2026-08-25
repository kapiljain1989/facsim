# Extending the simulator across the development lifecycle

A plan for growing `pharma_sim` from a manufacturing simulator into an
end-to-end **pharmaceutical development and manufacturing** simulator covering
formulation and analytical development, clinical development, and the existing
plant — with one identity graph running through all three.

The worked programme is **oncology**: a covalent KRAS G12C inhibitor in
non-small-cell lung cancer, from preformulation through a randomised Phase II to
submission-ready datasets.

> Status: **substantially built.** Phases 0 to 2 are done and all three spine
> links hold. What remains is the formulation DoE, a clinical safety domain, and
> the depth items in Phase 3. Section [11](#11-build-phases) records what is
> built against what was planned; the domain documentation is in
> [ANALYTICAL_DEVELOPMENT.md](ANALYTICAL_DEVELOPMENT.md) and
> [CLINICAL_DEVELOPMENT.md](CLINICAL_DEVELOPMENT.md).

---

## Contents

- [1. The fidelity bar](#1-the-fidelity-bar)
- [2. The worked programme](#2-the-worked-programme)
- [3. What already exists, and why it is the right base](#3-what-already-exists-and-why-it-is-the-right-base)
- [4. Target shape](#4-target-shape)
- [5. The spine: one identity graph](#5-the-spine-one-identity-graph)
- [6. Domain: formulation and analytical development](#6-domain-formulation-and-analytical-development)
- [7. Domain: clinical development](#7-domain-clinical-development)
- [8. Vendor adapters](#8-vendor-adapters)
- [9. The multi-timescale problem](#9-the-multi-timescale-problem)
- [10. Platform work this forces](#10-platform-work-this-forces)
- [11. Build phases](#11-build-phases)
  - [11.1 The determinism prerequisite](#111-the-determinism-prerequisite)
- [12. How we know it is credible](#12-how-we-know-it-is-credible)
- [13. Risks and open questions](#13-risks-and-open-questions)

---

## 1. The fidelity bar

The audience is someone with twelve to fifteen years inside these systems — a
person who has built oncology studies in Veeva Vault EDC or Rave, closed out an
eTMF for an inspection, or signed off a Result Set in Empower. That person does
not evaluate synthetic data by looking at column names. They look for the things
that only appear in real data.

**General tells**

| They look for | Random generators never have it |
|---|---|
| A query that was raised, answered, and **re-queried** because the site answered the wrong question | Query tables with one state transition |
| Data-entry lag that varies by site, and gets worse when the coordinator goes on leave | Entry date == visit date |
| An eTMF that is 94% complete because three sites never returned signed 1572s | Uniformly complete document sets |
| System suitability that **fails** on the second injection and the analyst re-runs the standard | Every injection passing |
| An OOS that resolves as Phase I laboratory error, and one that does not | No investigation records at all |
| A protocol amendment that forces a CRF version migration mid-study | One CRF version for the whole study |
| Stability trending that predicts the shelf life the label actually claims | Stability numbers unrelated to the expiry date |
| The analytical method that released the batch being **the same method** that was validated in development | Three unconnected datasets |

**Oncology tells** — these are the ones that separate credible oncology data
from generic clinical data:

| They look for | Why it is hard to fake |
|---|---|
| Target-lesion diameters whose **sum arithmetically produces** the assigned RECIST 1.1 response | Requires a lesion-level model, not a response column |
| An investigator-assessed PR that BICR reads as SD — with a **declared discordance rate** | Requires two independent readers over the same lesion data |
| PD triggered by a **new lesion** while the target-lesion sum is still shrinking | Requires non-target and new-lesion tracking |
| PFS censored because a subject had two consecutive missed assessments before PD | Requires the censoring rules to actually bite on the visit data |
| Tumour assessments on a **calendar schedule** (q6w from C1D1), not a visit schedule — drifting out of window | Oncology assessments are decoupled from dosing visits |
| Dose interruptions and reductions that follow the protocol's dose-modification table from a **CTCAE grade** | Requires graded AEs driving an exposure model |
| Relative dose intensity that falls in the arm with more Grade 3 diarrhoea | Requires exposure to be downstream of toxicity |
| A molecular pre-screening funnel: ~1,000 patients consented to find ~120 KRAS G12C randomisable | G12C prevalence is ~13%; most demo data has no pre-screen at all |
| Deaths before first post-baseline assessment, censored per the SAP not counted as PD | Edge cases are where reviewers look first |
| **Atropisomer** control on the drug substance, with an interconversion study | Specific to this chemical class; nobody guesses it |
| A **matching placebo** tested to confirm no API above LOQ — blind integrity | Only exists if you modelled a double-blind IMP |

The last general row matters most, and it is the reason for choosing a single
unified simulator rather than three siblings. Nobody's demo data has an identity
graph that survives from a DoE run in formulation, through method validation,
into the QC test that releases a commercial batch, into the blinded IMP kit
dispensed to a subject at a clinical site. That linkage is the differentiator.

---

## 2. The worked programme

### 2.1 Naming policy

Investigational molecule names are **fictional** and deliberately constructed to
conform to WHO INN stem conventions, so they read correctly to a domain reader
without colliding with a real product. Marketed standard-of-care agents are
named as real protocols name them — real generics, because a protocol that
invented a chemotherapy backbone would read as fake immediately.

| | |
|---|---|
| Investigational (fictional) | **Nelvorasib** — `-rasib` is the established stem for KRAS inhibitors |
| Standard of care (real generics) | carboplatin, pemetrexed, folic acid, vitamin B12, dexamethasone |
| Dictionaries | MedDRA and WHODrug **subsets** declared in config, not shipped dictionaries |

No real patient data, real study identifier, or real company name appears
anywhere. Every number is generated.

### 2.2 The molecule

**Nelvorasib** (development code **NVR-101**) — an orally administered, selective,
covalent inhibitor of KRAS G12C.

| Property | Value | Why it matters downstream |
|---|---|---|
| Modality | Small molecule, covalent (acrylamide warhead) | Forced-degradation profile is specific: acrylamide hydrolysis gives a characteristic degradant |
| Chirality | One stereocentre plus **atropisomerism** about a hindered biaryl axis | Forces a chiral/atropisomer HPLC method and an interconversion study — real for this class |
| BCS class | II — low solubility, high permeability; weak base, pH-dependent solubility | Multi-media dissolution, biorelevant media, food effect, IVIVC attempt |
| Potency | OEB 4 (highly potent) | Containment suite, PDE-based cleaning limits, dedicated equipment |
| Impurity risks | Pd residue from cross-coupling; NDSRI risk from a secondary amine | ICH Q3D (ICP-MS) and a ppb-level LC-MS/MS nitrosamine method |
| Regulatory frame | **ICH S9** (anticancer pharmaceuticals for advanced cancer) | Impurity qualification thresholds and nonclinical package differ from ICH M7 defaults |

### 2.3 The drug product

| | |
|---|---|
| Form | Film-coated immediate-release tablet, 60 mg |
| Dose | 240 mg once daily = 4 tablets |
| Route | Direct compression of a micronised crystalline API (Phase 1 DoE also explores a spray-dried amorphous solid dispersion) |
| Blinding | A **matching placebo** tablet, identical in appearance, size, debossing and film coat |
| Manufacture | A new dedicated containment suite in the existing plant — see [§6.6](#66-what-this-forces-on-the-existing-plant) |

### 2.4 The study

**NVR-101-201** — a randomised, double-blind, placebo-controlled Phase II study
of Nelvorasib in combination with platinum-doublet chemotherapy in previously
untreated advanced non-squamous NSCLC harbouring KRAS G12C.

| | |
|---|---|
| Population | Advanced/metastatic non-squamous NSCLC, KRAS G12C confirmed by central NGS, ECOG 0–1, no prior systemic therapy for advanced disease |
| Arms | **A:** Nelvorasib 240 mg QD + carboplatin AUC5 + pemetrexed 500 mg/m² · **B:** matching placebo + carboplatin AUC5 + pemetrexed 500 mg/m² |
| Randomisation | 2:1, stratified block via IRT — stratified by ECOG (0 / 1), region, PD-L1 TPS (<1% / ≥1%) |
| Cycles | 21 days. Four induction cycles, then maintenance (Nelvorasib or placebo + pemetrexed) until PD |
| Primary endpoint | PFS by RECIST 1.1 per **blinded independent central review** |
| Key secondary | ORR, DoR, DCR, OS, safety by CTCAE v5.0, PK |
| Tumour assessment | Baseline, then **q6w ±7d** to week 48, then q9w ±7d — calendar-driven, not visit-driven |
| Target N | 120 randomised (80 / 40) |
| Sites | 6 sites across 2 countries for the vertical slice |
| Oversight | Independent Data Monitoring Committee with two interim safety reviews |

Two deliberate simplifications, called out rather than hidden: a real Phase II of
this size runs across 40–60 sites, and would typically run in more than two
countries. Six sites in two countries is a slice reduction to keep Phase 2
tractable; [Phase 3](#phase-3--depth) scales it out. Everything else about the
design is what a real programme of this shape looks like.

---

## 3. What already exists, and why it is the right base

Roughly 17,000 lines, and four properties that the new domains need as much as
manufacturing did:

- **No domain vocabulary in Python.** States, event types, equipment classes,
  failure modes, QC parameters and products are all data in a runtime registry
  ([`registry/`](../src/pharma_sim/registry/)), validated by
  [`config/models.py`](../src/pharma_sim/config/models.py) and cross-checked by
  the 791-line [`config/linter.py`](../src/pharma_sim/config/linter.py). Oncology
  vocabulary is *far* larger than manufacturing's — RECIST criteria, CTCAE
  grades, cycle-based schedules, TMF artifact taxonomies, ICH stability
  conditions. None of it should become Python.
- **A deterministic discrete-event scheduler**
  ([`engine/scheduler.py`](../src/pharma_sim/engine/scheduler.py)) keyed on
  `(when, priority, sequence)`. It is already timestamp-based and unbounded, so a
  36-month stability pull, a q6w tumour assessment and a 60-second sensor tick
  coexist without a fixed-step loop oversampling any of them.
- **Per-entity named RNG streams**
  ([`engine/rng.py`](../src/pharma_sim/engine/rng.py)). `Random(f"{seed}:{name}")`
  means `site:DE-004:entry_lag` is stable regardless of what else runs. Adding
  clinical and laboratory domains will not perturb one byte of existing plant
  history — which is what makes the extension safe.
- **One schema declaration driving four backends**
  ([`storage/schema.py`](../src/pharma_sim/storage/schema.py)). New tables are
  declarations, not four parallel DDL edits.

Deterministic ids come from [`engine/ids.py`](../src/pharma_sim/engine/ids.py)
rather than `uuid4`, which is what will let the cross-domain identity graph be
reproducible.

**Nothing in the existing engine needs to be redesigned.** What it needs is to be
namespaced — see [§10](#10-platform-work-this-forces).

---

## 4. Target shape

```
pharma_sim/
  config/          loader, models, linter          — namespaced by domain
  registry/        runtime vocabularies            — bundled per domain
  engine/          clock, scheduler, bus, rng, ids — shared, unchanged
  domain/
    manufacturing/   plant, machine, sensor, batch, qc, maintenance, deviation   (exists, moved)
    lab/             substance, formulation, doe, method, validation, sample,
                     stability, injection, chromatogram, oos, instrument
    clinical/        protocol, study, country, site, subject, cycle, visit, form,
                     item, query, monitoring, lesion, response, safety, exposure,
                     coding, lock
    lifecycle/       the cross-domain identity graph and its invariants
  exports/
    adapters/
      cdisc/         ODM-XML, SDTM (incl. TU/TR/RS), ADaM (incl. ADRS/ADTR/ADTTE),
                     define.xml
      veeva/         Vault CTMS objects, eTMF (DIA TMF RM v3)
      edc/           Rave-style extract + ALS-style study spec + query log
      cds/           Empower-style Result Set, Chromeleon-style sequence,
                     LabX-style records, AnIML / Allotrope ASM
      ectd/          Module 3.2.P / 3.2.S / 5.3.5.1 skeleton
```

Config moves from sixteen flat files to namespaced directories:

```
config/
  platform/       plant-agnostic: storage, sinks, scenarios, event_types
  manufacturing/  the current fifteen files, plus the containment suite
  lab/            substances, excipients, formulations, doe, methods,
                  instruments, specifications, stability, cds, lims_workflow, eln
  clinical/       protocol, soa, crf, edit_checks, codelists, sites, countries,
                  enrolment, tumour, dose_modification, monitoring, safety, imp,
                  tmf_model, milestones, coding, lock, submission
  lifecycle/      the links: substance -> formulation -> IMP -> product -> batch
```

---

## 5. The spine: one identity graph

This is the first thing to build and the thing every later phase hangs off.

```
                      SUBSTANCE  Nelvorasib / NVR-101  (SUB-0001)
                                        |
        +-------------------------------+--------------------------------+
        |                                                                |
   FORMULATION (FRM-0001..n)                                      METHOD (MTH-0001..n)
   DoE: micronised DC vs spray-dried ASD                   assay & related substances,
   plus a MATCHING PLACEBO formulation                     atropisomer, dissolution,
        |                                                  KF, residual solvents,
        |  selected prototype                              Pd by ICP-MS, NDSRI by LC-MS/MS
        v                                                          |
   DRUG PRODUCT                                                    | validated (ICH Q2(R2))
   NVR-101 60 mg tablet  (PRD-NVR060)     <---- released by ----   v
   NVR-101 placebo tablet (PRD-NVRPBO)                        QC TEST (manufacturing)
        |                                                          ^
        |  made in the containment suite                           |
        v                                                    SPECIFICATION (SPC-...)
   BATCH (BATCH-2026-000042)  ---------- tested to ----------------+
        |
        +-- stability protocol --> ICH Q1A pulls --> trend --> shelf life --> label expiry
        |
        +-- QP release --> IMP LOT --> blinded kit build --> SHIPMENT --> SITE
                                                                   |
                                                              IRT assigns kit
                                                                   |
                                                                   v
                                                    SUBJECT DOSING (active or placebo)
                                                                   |
                                          +------------------------+------------------+
                                          |                                           |
                                    AE (CTCAE grade)                          LESIONS (RECIST 1.1)
                                          |                                           |
                                  dose modification                        investigator + BICR response
                                          |                                           |
                                          v                                           v
                                    SDTM EX / ADEX                            SDTM TU/TR/RS -> ADRS/ADTR
                                          |                                           |
                                          +---------------> ADTTE (PFS, OS, DoR) <-----+
                                                                   |
                                                     STUDY NVR-101-201 -> CSR
                                                                   |
                                                          eCTD m3.2.P / m5.3.5.1
```

Concrete invariants the engine must hold, each one checkable by
`verify-integrity`:

All three links now hold, checked by thirteen `verify-spine` checks. The
invariants below are stated as they were planned; where one is enforced
differently in practice that is noted.

1. Every `qc_results.method_id` in manufacturing resolves to a **validated**
   `lab.methods` row, and the QC result's precision is bounded by the
   intermediate precision established in that method's validation.
2. The Nelvorasib drug product traces to exactly one selected
   `lab.formulations` prototype, and its `process_parameters` are the DoE
   optimum, not free-floating numbers. The **placebo** product traces to the
   matched placebo formulation and shares the coating and debossing attributes.
3. Every batch's expiry equals `manufacture_date + shelf_life`, where
   `shelf_life` is derived from the stability trend for that formulation — not a
   configured constant.
4. Every `clin_dosing` row resolves to a kit, which resolves to a shipment,
   which resolves to a QP-released `batch_id` from the plant — and the kit's
   active/placebo identity matches the subject's IRT arm assignment, which is
   **not** visible in any blinded export until the unblinding event.
5. Every SDTM `EX` record reconciles to the IMP accountability log; every SDTM
   `LB` record for PK or central lab resolves to a `lab_samples` row.
6. A manufacturing OOS whose RCA cause is `method_variability` must point at a
   method whose validation robustness study actually shows sensitivity to the
   implicated parameter.
7. Every `RS` (response) record is **derivable** from the `TR` (tumour results)
   records for that subject and timepoint under RECIST 1.1 — for both the
   investigator and the BICR evaluator — and each ADTTE PFS record's censoring
   reason is reproducible from the visit and assessment data.

Invariants 6 and 7 are the ones that make a senior practitioner sit up. In 6 the
root-cause analysis is *correct*, because the weakness it names was generated
into the method's validation data before the OOS happened. In 7 the response
data is not a column of labels — it is arithmetic over lesion measurements, so
it survives being recomputed.

**Implementation:** a new `domain/lifecycle/graph.py` holding the link tables,
plus `config/lifecycle/links.yaml` declaring them, plus lint rules and
integrity checks. Link tables are first-class rows, not joins inferred at export.

---

## 6. Domain: formulation and analytical development

Built first in Phase 1, because it shares the most with the existing plant
(instruments behave like machines, OOS behaves like a deviation, specifications
behave like QC rules) and because the clinical domain depends on it for IMP.

### 6.1 Config vocabulary

| File | Declares |
|---|---|
| `substances.yaml` | APIs: MW, pKa, logP, pH-solubility profile, BCS class, polymorphs and hydrates, stereocentres and **atropisomer** axes with interconversion barrier, degradation pathways, hygroscopicity, OEB band |
| `excipients.yaml` | Functional class (filler, binder, disintegrant, lubricant, glidant), grades, known incompatibilities, and the colourants and coating premixes needed for placebo appearance matching |
| `formulations.yaml` | Prototype compositions (%w/w), dosage form, route (direct compression / wet granulation / spray-dried dispersion), and **placebo-match constraints** linking a placebo to its active |
| `doe.yaml` | Design type (full/fractional factorial, central composite, D-optimal), factors with ranges, responses, and the true response surface as declarative `Transfer` polynomials |
| `methods.yaml` | Methods by technique (HPLC-UV, chiral HPLC, LC-MS/MS, GC-HS, ICP-MS, UV, dissolution, KF titration), column, mobile phase, gradient table, detection, run time, expected retention times per analyte and degradant, true response factors |
| `instruments.yaml` | Instrument classes, vendor/model, CDS binding (Empower / Chromeleon / LabX), qualification state, calibration and PM intervals, drift and noise models |
| `specifications.yaml` | Test panels per product and stage, acceptance criteria under **ICH S9** thresholds, spec versions and effective dates |
| `stability.yaml` | ICH Q1A conditions and pull schedules, Q1B photostability, in-use and blinded-kit stability, degradation kinetics (Arrhenius parameters per condition) |
| `cds.yaml` | System suitability criteria per method, calibration design, sequence templates, audit-trail event vocabulary |
| `lims_workflow.yaml` | Sample lifecycle states and roles, review levels, OOS/OOT phase definitions, retest and resample rules |
| `eln.yaml` | Experiment templates, sections, witness/countersign rules, inventory consumption |

Note the pattern: `doe.yaml` and `methods.yaml` declare the **ground truth** —
the real response surface, the real response factor — and the engine then
observes it through instrument noise, analyst variability, calibration drift and
sample-preparation error. The generated data is a *measurement* of a truth that
exists, which is exactly why the numbers hang together under analysis.

### 6.2 Causal chain

```
substance properties (weak base, BCS II, atropisomeric, OEB 4)
  -> preformulation: pH-solubility profile, polymorph screen (XRPD/DSC/TGA),
       hygroscopicity, excipient compatibility, atropisomer interconversion
  -> formulation prototypes constrained by those results
       branch A: micronised crystalline, direct compression
       branch B: spray-dried amorphous solid dispersion
       plus:     matching placebo, appearance-constrained
  -> DoE: factor settings -> true response surface -> observed responses
       (hardness, friability, disintegration, multi-media dissolution, assay,
        content uniformity)
  -> selected prototype + optimum process parameters
  -> ANALYTICAL: method development
       column and gradient screening; forced degradation (acid, base, oxidative,
       thermal, photolytic) producing the acrylamide-hydrolysis degradant;
       peak purity and mass balance; atropisomer separation
     -> method validation (ICH Q2(R2)): specificity, linearity and range,
        accuracy, repeatability, intermediate precision, LOD/LOQ, robustness,
        solution stability, filter validation
     -> validated method characteristics: bias, %RSD, LOQ, robustness weak points
  -> LIMS: sample login -> aliquot -> test plan from specification -> schedule
       -> analyst and instrument assignment -> injection sequence
  -> CDS: sequence -> system suitability -> calibration standards -> samples
       -> chromatogram traces -> integrated peaks -> results
       -> second-person review -> approval -> CoA
  -> BLIND INTEGRITY: placebo batches assayed to confirm no API above LOQ
  -> STABILITY: Arrhenius degradation per condition -> timepoint results
       -> trending -> shelf-life regression -> label expiry -> kit expiry
  -> OOS when a result breaches spec: Phase I laboratory investigation
       (calculation error / dilution error / instrument fault / genuine)
       -> Phase II if unresolved -> retest / resample / invalidated assay
```

Every layer is downstream of the one above, which is the existing design
principle applied to the laboratory.

### 6.3 Data the CDS layer produces

The chromatogram is the piece that is genuinely hard and genuinely convincing.
A real chromatogram is not a list of peak areas; it is a signal trace, and the
peak table is what an integration algorithm found in it.

- `lab_chromatogram_points(injection_id, t_s, response)` in the **time-series
  store** — the same Timescale/ClickHouse path telemetry already uses. A 20-minute
  run at 5 Hz is 6,000 points; a full ICH Q2 validation of one method is roughly
  250 injections, so about 1.5 M points. Well inside what the existing store
  handles.
- Trace synthesis: exponentially-modified Gaussian per analyte, retention time
  from the method plus column-age and temperature drift, area from
  concentration × response factor × instrument response, plus baseline drift,
  detector noise, and — where the method's robustness study says so — resolution
  loss under the implicated condition. The atropisomer pair is a partially
  resolved doublet, which is what makes the chiral method interesting.
- `lab_peaks(...)` with retention time, area, height, width at half height,
  USP tailing, plate count, resolution from the preceding peak, signal-to-noise.
  Derived *from the trace*, not generated alongside it.
- `lab_system_suitability(...)` — replicate standard injections with %RSD,
  tailing and plate count against the criteria in `cds.yaml`, and a real failure
  rate that triggers a re-run and leaves both attempts in the audit trail.
- `lab_audit_trail(...)` — Part 11 shaped: object, action, old value, new value,
  reason for change, user, timestamp, signature meaning. Including the events
  that matter to an inspector: reprocessing, manual integration, aborted runs,
  and orphan data files.

### 6.4 Instrument integration

Modelled as its own failure-prone subsystem, because in real life it is:

- `lab_instrument_transfers(...)` — file-drop and driver-based acquisition with
  transfer success, parse failure, unmapped instrument, timestamp mismatch,
  duplicate file, orphan result, and queue backlog.
- Export adapters for **AnIML** and **Allotrope ASM**, plus a SiLA 2-style
  device command log, so integration work has something standards-shaped to
  target.

### 6.5 Tables (indicative, ~40)

`lab_substances`, `lab_polymorphs`, `lab_excipients`, `lab_formulations`,
`lab_formulation_components`, `lab_placebo_matches`, `lab_experiments` (ELN),
`lab_experiment_sections`, `lab_doe_designs`, `lab_doe_runs`, `lab_doe_responses`,
`lab_methods`, `lab_method_analytes`, `lab_method_dev_runs`,
`lab_forced_degradation`, `lab_validations`, `lab_validation_experiments`,
`lab_validation_results`, `lab_instruments`, `lab_instrument_calibrations`,
`lab_instrument_qualifications`, `lab_instrument_transfers`, `lab_columns`,
`lab_reagents`, `lab_standards`, `lab_samples`, `lab_aliquots`, `lab_test_plans`,
`lab_sequences`, `lab_injections`, `lab_peaks`, `lab_system_suitability`,
`lab_calibrations`, `lab_results`, `lab_result_reviews`, `lab_coa`,
`lab_blind_integrity`, `lab_stability_protocols`, `lab_stability_pulls`,
`lab_stability_results`, `lab_shelf_life`, `lab_oos`, `lab_oos_phases`,
`lab_audit_trail`, plus `lab_chromatogram_points` and `lab_dissolution_points`
in the time-series store.

### 6.6 What this forces on the existing plant

The shipped plant makes paracetamol, ibuprofen, amoxicillin, vitamin C and a
cefixime tablet at 250,000–300,000 unit batch sizes. Manufacturing an OEB 4
oncology API in those suites is the first thing a domain reader would object to —
cross-contamination and health-based exposure limits make it a non-starter.

So `config/manufacturing/` gains, in Phase 1:

- **A containment suite** — a new unit with its own dispensing booth, direct-
  compression line and coater, dedicated rather than shared, sized for clinical
  supply (batches of 20,000–60,000 tablets, not 300,000).
- **PDE-based cleaning validation** — cleaning limits derived from a
  health-based exposure limit rather than the 1/1000-dose heuristic the generic
  suites use, with swab and rinse results as QC tests.
- **Two products** — `PRD-NVR060` (active) and `PRD-NVRPBO` (matching placebo),
  sharing coating and debossing parameters so the blind holds.
- **Campaign scheduling** — the suite runs occasional campaigns rather than
  continuous production, which is also what makes the multi-timescale volume
  problem in [§9](#9-the-multi-timescale-problem) tractable.

This is config, not code. It is a good test of the no-vocabulary-in-Python
claim: a containment suite with different batch sizes, a different cleaning
regime and a placebo product should require zero engine changes.

---

## 7. Domain: clinical development

### 7.1 Config vocabulary

| File | Declares |
|---|---|
| `protocol.yaml` | Phase, indication, design, arms, blinding, randomisation ratio and stratification factors, objectives, endpoints (PFS/ORR/DoR/DCR/OS), inclusion/exclusion including biomarker eligibility, analysis populations, statistical assumptions, amendment history |
| `soa.yaml` | **Cycle-based** schedule: cycle length, cycle days (C1D1, C1D8, C1D15, C2D1…), procedures per cycle day, and **calendar-driven** assessment schedules (tumour imaging q6w then q9w) that run independently of dosing visits |
| `crf.yaml` | Casebook: forms, items, item groups, data types, units, codelist bindings, SDTM annotation per item, CRF versions |
| `edit_checks.yaml` | Declarative check expressions (range, cross-form, date-order, required-if, lesion-sum consistency), severity, query text, auto-close rules |
| `codelists.yaml` | Controlled terminology with CDISC CT bindings; ECOG, RECIST response, CTCAE grade |
| `countries.yaml` | Regulatory pathway and cycle times (CTA / IND), EC/IRB timelines, language, holiday calendars |
| `sites.yaml` | Site profiles: oncology capability, imaging capability, enrolment potential, staff, and **performance archetypes** (fast enroller / poor data quality / slow contract / high query rate) |
| `enrolment.yaml` | Two-step funnel: molecular pre-screening consent → central NGS → G12C prevalence (~13%) → main ICF → screening → screen failure or randomisation; screen-failure reason distribution; discontinuation hazards; seasonality |
| `tumour.yaml` | **New.** Lesion model: target and non-target lesion counts and organs, baseline diameters, per-arm growth and shrinkage kinetics, new-lesion hazard, RECIST 1.1 derivation rules, confirmation requirements, and the **investigator-versus-BICR discordance model** |
| `dose_modification.yaml` | **New.** Dose levels (240 / 180 / 120 mg), interruption and reduction rules keyed to CTCAE grade and AE term, re-escalation rules, permanent discontinuation criteria, chemotherapy dose-delay rules |
| `safety.yaml` | AE incidence by MedDRA SOC and PT with **CTCAE v5.0 grade distributions**, per arm; seriousness criteria; causality; AEs of special interest (hepatotoxicity, ILD/pneumonitis); SUSAR triggers; expedited reporting clocks; IDMC review triggers |
| `monitoring.yaml` | Visit types (SIV, IMV, COV, remote), frequency rules, SDV strategy (100% / risk-based / targeted with critical-data focus), finding taxonomies |
| `deviations.yaml` | Deviation categories including oncology-specific ones (assessment out of window, dose administered outside modification rules, eligibility violation), major/minor classification |
| `imp.yaml` | Blinded oral kits (active and matching placebo, indistinguishable), locally sourced chemotherapy, IRT randomisation and kit assignment, cycle-based resupply triggers, kit expiry and re-labelling, temperature excursion rates |
| `tmf_model.yaml` | DIA TMF Reference Model v3 zones, sections, artifacts; level (trial/country/site); expectedness rules driven by milestones; oncology artifacts (imaging charter, BICR charter, IDMC charter and minutes, DSUR, central lab and biomarker manuals, pharmacy manual, IRT specification) |
| `milestones.yaml` | Study and site milestone chains with dependencies and cycle-time distributions |
| `coding.yaml` | MedDRA and WHODrug dictionary subsets, CTCAE mapping, auto-coding hit rates, manual review and coder query rules |
| `lock.yaml` | Pre-lock checklist, soft and hard lock criteria, reconciliation scopes (safety database, central lab, imaging vendor, IRT, PK) |
| `submission.yaml` | SDTM domains, ADaM datasets, define.xml metadata, eCTD placement |

`tumour.yaml` and `dose_modification.yaml` are the two files that make this
oncology rather than generic clinical, and they are where the modelling effort
concentrates.

### 7.2 Causal chain

```
protocol + cycle-based SoA + CRF
  -> study build (CRF versions, edit checks, codelists, IRT specification)
  -> country regulatory + EC/IRB cycle times -> country activation
  -> site milestone chain: feasibility -> CDA -> budget -> contract
       -> EC submission -> EC approval -> green light -> SIV -> FPFV
  -> PRE-SCREENING: molecular consent -> tissue -> central NGS
       -> KRAS G12C positive (~13%) or not
  -> main ICF -> screening (imaging baseline, labs, ECOG, PD-L1 TPS)
       -> screen failure (reason distribution) | eligible
  -> randomisation via IRT, 2:1, stratified -> blinded kit assignment
  -> CYCLES of 21 days: dosing, cycle-day visits, labs, ECOG
       -> IMP accountability, returned and unused tablet counts
  -> LESION MODEL, on the calendar assessment schedule (q6w then q9w):
       target lesion diameters evolve per arm kinetics
       non-target lesions assessed categorically
       new lesions arise on a hazard
       -> SLD computed -> RECIST 1.1 response derived
       -> derived TWICE: investigator read, and BICR read with a declared
          discordance model
       -> PD by BICR ends treatment; PFS event or censor per the SAP rules
  -> SAFETY: AEs drawn per arm by SOC/PT with CTCAE grades
       -> serious ones become SAEs -> SUSAR assessment -> reporting clock
       -> AEs of special interest escalate to IDMC review
  -> EXPOSURE: dose_modification rules read CTCAE grades
       -> interruptions, reductions, delays -> relative dose intensity
       -> RDI differs by arm because toxicity differs by arm
  -> DATA FLOW: forms entered with site- and coordinator-specific lag
       -> edit checks fire -> system queries
       -> DM and CRA raise manual queries; lesion-sum inconsistencies
          generate a characteristic query cluster
       -> query lifecycle: open -> answered -> closed | re-queried
  -> monitoring visits -> risk-based SDV of critical data -> findings
       -> action items -> some findings become protocol deviations
  -> medical coding: MedDRA for AEs, WHODrug for concomitant medications
       -> auto-code hits, manual review, coder queries
  -> eTMF: milestones drive the expected document list; documents arrive
       (or do not) with realistic lag; completeness and timeliness KPIs emerge
  -> reconciliation: SAE (EDC vs safety database), imaging vendor vs EDC,
       central lab, IRT, PK
  -> query burn-down -> pre-lock checklist -> soft lock -> hard lock
       -> UNBLINDING: kit active/placebo identity becomes visible
  -> SDTM: DM AE CM EX DS LB VS EG SC MH SV SE + TU TR RS + PC PP
       + trial design TA TE TI TS
  -> ADaM: ADSL ADAE ADLB ADVS ADEX ADTR ADRS ADTTE
  -> TLFs -> CSR -> define.xml + cSDRG/ADRG -> eCTD m5.3.5.1
```

The valuable part is that the operational data and the submission data are the
**same data**. An analyst can take an ADTTE PFS record, walk back to the RS
response, back to the TR lesion measurements, back to the eCRF item, back to the
query that corrected a transcription error, and find the monitoring visit where
the CRA caught it. That traceability is what nobody's sample data offers.

### 7.3 Deliberate imperfection

Realism here *is* imperfection, generated on purpose and recorded as ground
truth so it can be scored against:

- Sites whose entry lag degrades for six weeks (staff turnover), then recovers.
- One site with a query rate three times the study mean, which triggers a
  for-cause monitoring visit.
- One site with a systematic lesion-measurement bias, caught by BICR
  discordance before it is caught by monitoring.
- A protocol amendment at month 8 adding a mandatory ophthalmic assessment,
  forcing a CRF version migration and leaving pre-amendment subjects on the old
  version.
- Two subjects randomised against inclusion criteria — major deviations, one
  leading to exclusion from the per-protocol population.
- Three subjects whose tumour assessment slipped outside the ±7-day window,
  one of whom then has two consecutive missed assessments and is censored rather
  than counted as a PFS event.
- Two subjects who died before any post-baseline tumour assessment.
- An eTMF that reaches 94% completeness with a specific, nameable set of gaps.
- One SAE that took eleven days to reconcile between EDC and the safety
  database.
- One temperature excursion on a kit shipment, quarantined and replaced.

Each of these lands in the existing `ground_truth` and `prediction_labels`
tables ([`domain/ground_truth.py`](../src/pharma_sim/domain/ground_truth.py)),
so the dataset is immediately usable for evaluating risk-based monitoring
models, query-prediction models, site-risk scoring and enrolment forecasting.

### 7.4 Tables (indicative, ~45)

`clin_studies`, `clin_protocol_versions`, `clin_arms`, `clin_epochs`,
`clin_cycles_planned`, `clin_visits_planned`, `clin_assessment_schedules`,
`clin_procedures`, `clin_forms`, `clin_items`, `clin_codelists`,
`clin_edit_checks`, `clin_crf_versions`, `clin_countries`, `clin_sites`,
`clin_site_milestones`, `clin_site_staff`, `clin_prescreening`,
`clin_biomarkers`, `clin_subjects`, `clin_screening`, `clin_randomisation`,
`clin_cycles_actual`, `clin_visits_actual`, `clin_form_instances`,
`clin_item_data`, `clin_item_audit`, `clin_queries`, `clin_query_events`,
`clin_sdv`, `clin_monitoring_visits`, `clin_monitoring_findings`,
`clin_action_items`, `clin_deviations`, `clin_lesions`,
`clin_lesion_assessments`, `clin_responses`, `clin_adverse_events`,
`clin_sae_reports`, `clin_dose_modifications`, `clin_exposure`,
`clin_concomitant_meds`, `clin_coding`, `clin_pk_samples`,
`clin_imp_shipments`, `clin_imp_kits`, `clin_dosing`,
`clin_imp_accountability`, `clin_temperature_excursions`, `clin_idmc_reviews`,
`clin_tmf_artifacts`, `clin_tmf_documents`, `clin_tmf_document_versions`,
`clin_reconciliation`, `clin_lock_events`, `clin_unblinding`,
`clin_datasets_sdtm`, `clin_datasets_adam`.

`clin_lesions` and `clin_lesion_assessments` are the load-bearing pair: every
response, every PFS event and every ADTTE row is arithmetic over them.

---

## 8. Vendor adapters

One canonical truth, exported into the shapes practitioners recognise. Adapters
are pure functions over the persisted tables — no simulation logic, so they can
be added and changed without touching the engine.

| Adapter | Produces |
|---|---|
| `cdisc/odm` | ODM-XML 1.3.2 study definition (MetaDataVersion, ItemGroupDef, ItemDef, CodeList) plus clinical data with `AuditRecord` |
| `cdisc/sdtm` | SDTM 3.4 domains as XPT and Parquet, including the oncology trio **TU / TR / RS** with `RSEVAL` distinguishing investigator from independent assessor |
| `cdisc/adam` | ADaM 1.3: ADSL, ADAE, ADLB, ADVS, ADEX, **ADTR**, **ADRS**, and **ADTTE** with `PARAMCD` in (PFS, OS, DOR, TTR), `CNSR` and `EVNTDESC` |
| `cdisc/define` | define.xml 2.1 plus cSDRG and ADRG skeletons |
| `veeva/ctms` | Vault-shaped objects: Study, Study Country, Study Site, Study Person, Monitoring Event, Monitoring Event Report, Issue, Deviation, Subject, Milestone, Payable Item — as the Vault Loader consumes |
| `veeva/etmf` | eTMF document metadata against DIA TMF RM v3 zone/section/artifact, with EDL expectedness and completeness reporting |
| `edc/rave` | Architect-style study spec (ALS-shaped workbook), clinical data extract, query detail listing, audit trail |
| `edc/clinion` | The same content in a lean SaaS-EDC export shape |
| `cds/empower` | Project → Sample Set Method → Sample Set → Result Set → Result hierarchy, Method Sets, custom fields, audit trail, signoff |
| `cds/chromeleon` | Data Vault → folder → sequence → injection hierarchy, instrument and processing methods, report templates, audit trail |
| `cds/labx` | Balance, titrator and pH records in LabX task/result shape |
| `cds/animl`, `cds/asm` | AnIML documents and Allotrope Simple Model JSON for the same injections |
| `ectd/skeleton` | Module 3.2.S / 3.2.P (including 3.2.P.8 stability) and Module 5.3.5.1 folder structure with the generated artefacts placed |

Adapters ship with a `--validate` path where a public validator exists (for
example CDISC conformance rule subsets), so the export is checkable rather than
merely plausible.

---

## 9. The multi-timescale problem

This is the one genuine architectural tension. Manufacturing runs at 60-second
telemetry; the study runs three to five years; stability runs 36 months. Five
years of 60-second telemetry across 584 sensors is roughly 1.5 billion rows,
which is not a sensible default.

The scheduler already handles arbitrary cadences. The problem is **volume**, and
the fix is scoping rather than a new clock:

1. **Domain activation.** `run --domains clinical,lab` runs only those domains.
   The identity graph still resolves, because links point at ids that the spine
   can materialise as stubs from `config/lifecycle/links.yaml` without
   simulating the whole plant.
2. **Campaign windows.** Manufacturing declares campaign windows in
   `lifecycle/links.yaml` — the plant produces full-fidelity telemetry only
   inside the windows where it is actually making IMP or commercial batches, and
   is idle between them. This is also *true* of a clinical-supply containment
   suite, so the realism and the volume fix point the same way. A five-year
   lifecycle with six two-week campaigns is about 15 million telemetry rows, not
   1.5 billion.
3. **Per-domain resolution.** Each domain declares its own cadence in config:
   sensors at 60 s, chromatograms at 5 Hz within a run, clinical events at day
   resolution, tumour assessments on the q6w/q9w calendar, stability at the ICH
   pull schedule.
4. **`--resolution` flag** for whole-lifecycle runs: `full` (everything),
   `records` (skip raw telemetry and chromatogram traces, keep every derived
   record), `summary` (aggregates only). A five-year `records` run should
   complete in minutes and fit comfortably in SQLite.

`Priority` in [`engine/scheduler.py`](../src/pharma_sim/engine/scheduler.py)
gains bands above the current `PERSIST = 120` for the slower domains, so
intra-day manufacturing ordering is untouched:

```
CLINICAL_MILESTONE = 200   CLINICAL_CYCLE   = 205   CLINICAL_VISIT = 210
CLINICAL_ASSESS    = 215   CLINICAL_DATA    = 220   CLINICAL_QUERY = 230
LAB_SAMPLE         = 300   LAB_INJECTION    = 310   LAB_RESULT     = 320
STABILITY_PULL     = 400   LIFECYCLE        = 500
```

`CLINICAL_ASSESS` sits after `CLINICAL_VISIT` deliberately: a tumour assessment
must resolve after any dosing visit that shares its date, so that an AE recorded
that day can already have influenced the exposure model.

---

## 10. Platform work this forces

Five changes to shared code. All are mechanical, all are covered by the existing
test suite, and none change generated manufacturing output — which is the
acceptance criterion for Phase 0.

1. **Namespaced config.** `CONFIG_FILES` in
   [`config/models.py`](../src/pharma_sim/config/models.py) becomes a nested map
   of `domain -> {stem: field}`, and `load_config` walks
   `config/<domain>/<stem>.yaml`. A compatibility shim keeps flat layouts
   loading, so existing configs and `config/examples/minimal_factory/` keep
   working. `FactoryConfig` becomes `LifecycleConfig` with `manufacturing`,
   `lab`, `clinical`, `lifecycle` and `platform` sub-models; `FactoryConfig`
   stays as an alias.
2. **Modular schema.** [`storage/schema.py`](../src/pharma_sim/storage/schema.py)
   splits into `schema/manufacturing.py`, `schema/lab.py`, `schema/clinical.py`,
   `schema/lifecycle.py`, `schema/platform.py`, merged into `TABLES` with a
   `TABLE_ORDER` computed by topological sort over `references` rather than
   hand-maintained. At ~115 tables, hand-ordering stops being safe.
3. **Registry bundles.** `Registries` gains `lab: LabRegistries` and
   `clinical: ClinicalRegistries` sub-bundles rather than growing flat.
4. **Linter extension.** [`config/linter.py`](../src/pharma_sim/config/linter.py)
   is the safety net for the no-enum design, so every new cross-reference needs
   a rule: form → item → codelist, item → SDTM variable, cycle day → procedure,
   assessment schedule → RECIST timepoint, lesion organ → anatomical codelist,
   dose level → arm, dose-modification rule → CTCAE grade → AE term, artifact →
   zone → section, method → instrument → column, DoE factor → process
   parameter, stability condition → ICH condition, milestone → predecessor,
   placebo formulation → active formulation. Expect this file to roughly double.
5. **Id vocabulary.** [`engine/ids.py`](../src/pharma_sim/engine/ids.py) gains
   accessors for the new entities — `subject()`, `lesion()`, `kit()`,
   `injection()`, `sample()`, `query()`, `tmf_document()` — keeping the "call
   sites read as domain language" convention.

---

## 11. Build phases

### Phase 0 — Spine and platform (foundation)

- **Cross-process determinism.** Done first, because everything below is
  acceptance-tested by comparing digests, and a digest that is not stable
  against itself proves nothing. See
  [§11.1](#111-the-determinism-prerequisite).
- `scripts/phase0_digest.py` — the acceptance harness: runs the simulation into
  a scratch directory, exports CSV, and hashes the content with the three
  wall-clock columns blanked. `--compare` diffs two manifests file by file.
- Namespaced config with the compatibility shim; modular schema with topological
  `TABLE_ORDER`; registry bundles; new priority bands; new id accessors.
- `domain/lifecycle/graph.py` and `config/lifecycle/links.yaml`.
- `--domains` and `--resolution` flags on `run`.
- **Acceptance:** `pytest` green, and a 30-day run at the current seed produces a
  byte-identical export digest to the current `master`. The extension must not
  perturb existing history.

#### 11.1 The determinism prerequisite

Building the harness immediately found that **the simulator's output depended on
`PYTHONHASHSEED`**. Two runs of the same config at the same seed produced
different row counts — 78 batches against 76, 2,607 state intervals against
2,443 — because of one expression in
[`domain/plant.py`](../src/pharma_sim/domain/plant.py):

```python
classes_here = tuple({group.equipment_class for group in ...})
```

A set comprehension materialised into a tuple. Set iteration order for strings
varies per process, and a few lines later that tuple is zipped against RNG
draws:

```python
certified = tuple(c for c in classes_here if rng.random() < 0.82)
```

The *number* of draws was stable, so the seed looked respected — but a different
equipment class received each draw in every process. Different certifications
mean different operator assignment, which means different production, which
cascades through the whole run. It contradicted the central claim in
[`engine/rng.py`](../src/pharma_sim/engine/rng.py) that named streams make a run
reproducible regardless of interleaving.

The fix is `dict.fromkeys` instead of a set: deduplicate while preserving the
declaration order from `units.yaml`, which is both deterministic and the order a
reader of the config expects. Sorting would also have been deterministic, but it
would have imposed an order that appears nowhere in the configuration.

Two things are worth drawing out of this, because they shape the rest of the
plan:

1. **In-process tests structurally cannot catch this.** `PYTHONHASHSEED` is
   fixed for the life of an interpreter, so two builds in one process always
   agree. `tests/test_determinism.py` shells out with the seed set explicitly,
   and pairs that with a static AST guard against the pattern — an ordered
   container built from a `set` or `frozenset` anywhere in the package. Both
   tests were confirmed to fail against the unfixed code before being kept.
2. **The exposure grows with every new domain.** Manufacturing had one instance
   of the pattern. The clinical and laboratory domains will introduce far more
   collection-shuffling — lesion sets, certified analyst pools, expected-document
   lists, kit pools — and each is a place where the same class of bug pairs the
   wrong entity with the wrong draw. The AST guard is cheap insurance, and it
   belongs in CI before the new domains are written, not after.

### Phase 1 — Laboratory vertical slice

**Partly built.** The analytical half is done and documented in
[ANALYTICAL_DEVELOPMENT.md](ANALYTICAL_DEVELOPMENT.md): `config/lab/` (five
files), `pharma_sim.lab` (chromatography, method model, ICH Q2 runner) and
`scripts/generate_lab_dataset.py`, which produces 243 injections, 586 peaks, 49
evaluated criteria, 550 audit events and 1.09 M chromatogram points in about
thirty seconds, under 85 tests. The robustness study discovers the method's weak
point rather than being told it. Still to do: the formulation DoE, the LIMS
sample lifecycle, ICH Q1A stability with a shelf-life regression, the OOS
investigation, blind-integrity testing, the containment suite in
`config/manufacturing/`, and the Empower and Allotrope adapters.

Nelvorasib: one substance, two formulation branches plus a matching placebo, one
HPLC assay and related-substances method.

- Preformulation (pH-solubility, polymorph screen, atropisomer interconversion)
  → 8-run fractional-factorial DoE per branch → selected prototype.
- Method development including forced degradation → full ICH Q2(R2) validation,
  ~250 injections with real chromatogram traces, system suitability including
  realistic failures.
- LIMS lifecycle for one ICH Q1A stability protocol: 3 conditions × 6 timepoints
  × 3 batches, with trending and a shelf-life regression.
- One OOS at 40°C/6M dissolution, resolved through Phase I and Phase II.
- Placebo blind-integrity testing.
- The containment suite, PDE cleaning limits and the two new products added to
  `config/manufacturing/` — [§6.6](#66-what-this-forces-on-the-existing-plant).
- Adapters: `cds/empower`, `cds/asm`.
- **Acceptance:** the assay method's validated %RSD and bias measurably bound the
  QC precision the plant sees; stability trending produces the shelf life that
  batch expiry dates use; placebo and active share coating and debossing
  parameters.

### Phase 2 — Clinical vertical slice

**Built**, and beyond the original slice: the RECIST lesion model with dual
evaluator assessment, PFS with reproducible censoring, and then the whole
operational layer — sites with performance archetypes driving enrolment, CRF
forms and items over the existing visits, edit checks firing queries with
re-query, risk-based SDV, monitoring with a for-cause trigger, protocol
deviations, an eTMF at 92% complete, and database lock through to unblinding.
Documented in [CLINICAL_DEVELOPMENT.md](CLINICAL_DEVELOPMENT.md). Not built: the
safety domain, medical coding, and the molecular pre-screening funnel.


Study NVR-101-201 on the drug product from Phase 1.

- 2 countries, 6 sites with distinct performance archetypes, molecular
  pre-screening funnel of ~1,000 to randomise 120, 21-day cycles, 6 forms,
  ~40 items, ~25 edit checks.
- Full lesion model: target and non-target lesions, new-lesion hazard, RECIST 1.1
  derivation for both investigator and BICR with a declared discordance rate.
- Safety with CTCAE v5.0 grades driving `dose_modification.yaml` into
  interruptions, reductions and relative dose intensity that differs by arm.
- Operational data: visits, form entry with lag, queries with re-query,
  risk-based SDV, 12 monitoring visits, 6 deviations, 2 SAEs, 2 IDMC reviews,
  MedDRA and WHODrug coding.
- eTMF: ~70 expected artifacts across 4 TMF zones including the oncology
  artifacts, reaching ~94% completeness.
- Database lock: reconciliation across safety, imaging vendor, central lab, IRT
  and PK; query burn-down; soft then hard lock; **unblinding**.
- Submission: SDTM DM/AE/CM/EX/DS/LB/VS + **TU/TR/RS** + trial design;
  ADSL/ADAE/ADTR/ADRS/ADTTE; define.xml; XPT.
- IMP: blinded kits from a Phase-1-released batch, shipped, dispensed,
  accounted for, one temperature excursion.
- Adapters: `cdisc/sdtm`, `cdisc/adam`, `cdisc/define`, `veeva/ctms`,
  `veeva/etmf`.
- **Acceptance:** every SDTM `EX` record reconciles to the accountability log and
  back to a plant batch; every `RS` record recomputes from `TR` under RECIST 1.1
  for both evaluators; every ADTTE censoring reason is reproducible from the
  visit data; `verify-integrity` passes all seven spine invariants.

### Phase 3 — Depth

A Phase I dose-escalation study on the same substance (DLT windows, 3+3 or BOIN,
RP2D selection) and a Phase III, both sharing `SUB-0001`; scale the Phase II to
40+ sites and 6 countries; more methods (dissolution, KF, residual solvents, Pd
by ICP-MS, NDSRI by LC-MS/MS, atropisomer by chiral HPLC); Chromeleon and LabX
adapters; Rave and Clinion adapters; instrument-integration failure modes;
risk-based monitoring ground truth; a real safety-database entity; TLF and CSR
skeletons; eCTD assembly.

### Phase 4 — Surface

Dashboard and API extended to the new domains; `docs/CONFIGURING_A_STUDY.md` and
`docs/CONFIGURING_A_METHOD.md` in the style of the existing
[configuration guide](CONFIGURING_A_FACTORY.md); worked minimal examples for both
domains — including a **non-oncology** minimal study — proving the
no-vocabulary-in-Python claim holds and that oncology is configuration rather
than a special case in the engine.

---

## 12. How we know it is credible

Four gates, in increasing strictness:

1. **Referential integrity.** `verify-integrity` extended to the new tables and
   the seven spine invariants. Every row resolves.
2. **Statistical plausibility.** A `pharma_sim verify-realism` command checking
   generated distributions against declared expectations. A number outside its
   declared envelope is a build failure, not a curiosity:
   - ORR and median PFS per arm within the envelope `protocol.yaml` declares
   - target-lesion SLD kinetics consistent with the declared per-arm model
   - investigator-versus-BICR discordance within the declared rate
   - CTCAE grade distribution per PT within the declared profile
   - relative dose intensity lower in the arm with more Grade 3+ toxicity
   - pre-screening funnel yielding ~13% G12C positivity
   - query rates per form, enrolment against plan, TMF completeness
   - system-suitability %RSD, stability degradation against Arrhenius
3. **Standards conformance.** CDISC conformance rule subsets over SDTM
   (including TU/TR/RS), ADaM and define.xml; schema validation for ODM-XML,
   AnIML and ASM. Where a public validator exists, run it in CI.
4. **Domain review.** The gate that actually matters. A structured walkthrough
   with a practitioner from each domain, against the checklists in
   [§1](#1-the-fidelity-bar). The specific question to ask is not "does this look
   right" but "show me the thing that tells you this is synthetic" — and then fix
   that.

---

## 13. Risks and open questions

**Risks**

- *The lesion model is the highest-value and highest-effort piece.* Getting
  RECIST 1.1 derivation, confirmation rules, new-lesion handling, BICR
  discordance and PFS censoring all mutually consistent is the hardest modelling
  in the plan. Mitigation: build it standalone in Phase 2 with a property test
  that recomputes every `RS` from `TR`, before any of the operational layers sit
  on top.
- *Chromatogram synthesis is a research task.* Traces that a chromatographer
  finds convincing — correct peak shape, realistic baseline, plausible degradant
  profile, a partially resolved atropisomer doublet — is the highest-uncertainty
  item in the laboratory domain. Mitigation: build it first in Phase 1 and put it
  in front of a practitioner before anything else is built on top.
- *Config surface explosion.* Roughly thirty new YAML files is a lot to author
  and a lot to keep consistent. Mitigation: the linter is extended in the same
  commit as each new file, never after; each domain ships a minimal example
  alongside the full one.
- *The linter becomes the bottleneck.* It is already 791 lines and load-bearing.
  Mitigation: refactor it into per-domain rule modules with a shared reference
  resolver before adding rules, not after.
- *Oncology leaks into the engine.* The risk of committing to one therapeutic
  area is that RECIST and CTCAE end up hard-coded. Mitigation: the Phase 4
  non-oncology minimal study is not a nice-to-have — it is the test that the
  abstraction held.
- *Scope.* Either new domain is comparable in size to the existing manufacturing
  simulator. The thin-slice-first sequencing is the control.

**Resolved**

- *Therapeutic area:* **oncology**, as above. It costs more modelling effort than
  a respiratory or metabolic indication, but it buys the endpoint structures
  (TU/TR/RS, ADTTE, BICR) that are the most recognisable and least fakeable part
  of clinical data — and it is where the domain-facing roles this dataset is
  meant to serve actually sit.
- *XPT as a first-class export:* yes, in Phase 2. It is what submissions use, and
  its absence is noticeable to a submission practitioner. `pyreadstat` handles it.

**Open**

- Should iRECIST be modelled alongside RECIST 1.1? It is only relevant if an
  immunotherapy arm is added. *Leaning: no in Phase 2; add it in Phase 3 with a
  checkpoint-inhibitor combination study, where pseudoprogression makes it
  meaningful.*
- Does the clinical domain need a separate safety database (Argus/ArisG-shaped),
  or is SAE reconciliation adequately modelled as a reconciliation table against
  an implied external system? *Leaning: reconciliation table in Phase 2, real
  safety-database entity in Phase 3.*
- Is the imaging vendor a first-class entity? BICR runs through a separate
  imaging CRO with its own charter, read paradigm and adjudication. Modelling it
  as an entity would make the discordance and reconciliation stories much
  stronger. *Leaning: yes — the read and adjudication events are cheap to add
  once the lesion model exists.*
- How far to take eCTD? A full validated backbone is a large piece of work with
  little marginal credibility over a correct folder structure with correct
  artefacts in the right places. *Leaning: structure and placement only.*
- Does the containment suite need its own environmental-monitoring model
  (differential pressure, air changes) to be credible for a potent compound?
  *Leaning: yes, but it is a small sensor-config addition rather than new code.*
