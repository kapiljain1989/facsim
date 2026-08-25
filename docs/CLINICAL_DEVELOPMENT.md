# The clinical development domain

Generates a randomised oncology trial end to end: site activation, enrolment,
tumour assessment, case report forms, queries, monitoring, the trial master file,
database lock, and CDISC SDTM and ADaM output. Plus the investigational product
chain, from a batch the plant released to the kit a subject was dispensed.

```bash
.venv/bin/python scripts/generate_clinical_dataset.py --output data/clinical \
    --manufacturing-export data/plant --lab-export data/lab
```

Two seconds, and you get 27 tables:

| | |
|---|---|
| Sites and activation | 6 sites, 60 milestone dates |
| Subjects | 120, randomised 2:1 by permuted block |
| Tumour data | 1,912 `TU`, 7,184 `TR`, 1,726 `RS` — SDTM |
| Survival | 240 `ADTTE` records, two per subject |
| Case report forms | 5,901 forms, 30,142 item values |
| Queries | 330 queries, 1,122 lifecycle events, 132 audited corrections |
| Monitoring | 48 visits, 102 findings, 10,740 SDV records |
| Deviations | 61, across seven categories |
| Safety | 529 adverse events with CTCAE grades, 98 dose modifications, exposure and relative dose intensity per subject |
| Trial master file | 165 documents, 92% complete, 83% filed on time |
| Investigational product | 234 lots, 1,920 kits, 1,519 doses |
| Lock | 5 reconciliations, 5 lock events ending in unblinding |

The `--manufacturing-export` and `--lab-export` arguments are optional. Without
them the dataset is still complete and internally consistent, but it says so:
batches are labelled `STUB` and lot expiry records `DECLARED` rather than
`STABILITY`.

---

## Contents

- [The one idea that matters](#the-one-idea-that-matters)
- [The study](#the-study)
- [The configuration files](#the-configuration-files)
- [RECIST: the three details that decide everything](#recist-the-three-details-that-decide-everything)
- [Where reader disagreement comes from](#where-reader-disagreement-comes-from)
- [How a site's character propagates](#how-a-sites-character-propagates)
- [Safety, and why the grade matters](#safety-and-why-the-grade-matters)
- [The investigational product chain](#the-investigational-product-chain)
- [Reading the output](#reading-the-output)
- [Adding a study](#adding-a-study)
- [Limitations](#limitations)

---

## The one idea that matters

**Response is arithmetic over measured lesion diameters, not a column of
labels.**

`tumour.yaml` declares how a subject's disease behaves. The lesion model gives
them actual lesions on a biexponential trajectory. A radiologist measures those
lesions, to the millimetre, with their own error. Then RECIST 1.1 is applied to
the measurements — twice, once for the investigator and once for blinded
independent central review.

So the response column can be recomputed from the exported `TR` rows, and a test
does exactly that for every subject and both evaluators. That is what makes the
dataset survive a statistician who recomputes something, and it is the difference
between data that holds together under analysis and data that merely looks
plausible in a spreadsheet.

The same principle runs through the operational layer. A tumour assessment form
carries the sum of diameters the lesion model computed, so all of the recorded
`SUMDIAM` values reconcile to SDTM `RS` exactly. Generated separately, the case
report form and the datasets would each be individually plausible and mutually
inconsistent — which is the failure that makes synthetic clinical data useless
for anything that joins across systems.

---

## The study

**NVR-101-201** — a randomised, double-blind, placebo-controlled Phase II study
of **Nelvorasib** in combination with platinum-doublet chemotherapy in previously
untreated advanced non-squamous NSCLC harbouring KRAS G12C.

| | |
|---|---|
| Arms | Nelvorasib 240 mg once daily + carboplatin/pemetrexed, against matching placebo + carboplatin/pemetrexed, 2:1 |
| Primary endpoint | Progression-free survival by RECIST 1.1 per blinded independent central review |
| Cycles | 21 days |
| Tumour assessment | Every 6 weeks to week 48, then every 9 weeks — on the **calendar** from randomisation, not on the dosing schedule |
| Data cut | Week 108, while subjects are still on treatment |

Nelvorasib is fictional. The name follows the WHO INN stem `-rasib`, used for
KRAS inhibitors, so it reads correctly to a domain reader without colliding with
a real product. The chemotherapy backbone is named with the real generics a
protocol of this shape would use, because an invented one would read as fake
immediately. No real patient data, study identifier or company name appears
anywhere.

The design is double-blind rather than the more obvious open-label single-agent
comparison, and that choice earns three things: the plant makes both an active
and a matching placebo tablet, kit assignment is genuinely blinded, and there is
a real unblinding event at database lock.

Two simplifications, called out rather than hidden: a real Phase II of this size
runs across 40–60 sites rather than six, and in more than two countries. Six
sites keeps the dataset tractable. Everything else about the design is what a
programme of this shape looks like.

---

## The configuration files

Six files under `config/clinical/`. No Python holds a RECIST threshold, a CTCAE
grade, a TMF artifact or an edit check.

| File | Declares |
|---|---|
| `protocol.yaml` | Phase, indication, arms and allocation, endpoints, cycle length, enrolment plan, analysis cut-off |
| `tumour.yaml` | RECIST 1.1 thresholds, the reader models, organ sites, baseline lesion distributions, biexponential growth per arm, new-lesion and death hazards, the assessment schedule |
| `sites.yaml` | Countries with regulatory cycle times, site performance archetypes, the sites, the activation milestone chain, staff turnover |
| `crf.yaml` | Forms and items with SDTM annotation, codelists, edit checks, query behaviour, and where every item's value comes from |
| `monitoring.yaml` | Visit types and triggers, the for-cause threshold, risk-based SDV strategy, finding taxonomy, deviation categories |
| `tmf_model.yaml` | DIA TMF Reference Model v3 zones and artifacts, expectedness by milestone, arrival lag and missing rates |

Cross-file references are plain strings by design, so the loader lints them: an
edit check naming an item no form has, a site archetype that does not exist, a
milestone whose predecessor is declared after it, an artifact in an undeclared
zone, an item with no value source. Each of those was confirmed to fail on a
deliberately broken config.

---

## RECIST: the three details that decide everything

Almost every incorrect implementation of RECIST gets one of these wrong, so each
has a test named after the failure it prevents.

**1. Partial response is measured against baseline; progression against the
nadir.**

A subject whose tumour sum falls 60% and then regrows 25% from its smallest value
**has progressed**, even though the sum is far below where it started. The
emitted dataset contains 28 such assessments — not many, because assessment stops
at documented progression, so each subject contributes at most one. Comparing
progression to baseline instead turns every one of them into a continuing partial
response, and that subject never progresses at all — so the median PFS comes out longer than the
study could possibly support and nothing looks obviously wrong.

Look for rows in `rs.csv` where `PCHGBASE` is strongly negative and `RSSTRESC`
is `PD`. `NADIR` and `PCHGNADIR` are carried on every row so the derivation can
be checked without recomputing it.

**2. Progression needs both a relative and an absolute increase.**

A 20% rise on a 12 mm sum is 2.4 mm, which is inside measurement error. RECIST
requires at least 5 mm as well. Without the absolute floor, small-burden subjects
progress on noise.

**3. Complete response is not "the sum reached zero".**

Nodal lesions do not disappear, they return to normal size. A node still
measurable at 9 mm short axis is compatible with complete response; one at 11 mm
is not. The sum can be non-zero and the response still `CR`.

There is also a fourth thing worth knowing, which is in the guideline and
surprises people: a complete response in the target lesions while non-target
disease persists is a **partial** response overall.

---

## Where reader disagreement comes from

The dataset carries two independent assessments of the same scans, and they
disagree — about 10% of paired visits, and the PFS date differs by more than one
assessment interval for roughly a third of subjects. Both are in the range real
trials report.

Getting that right took correcting a wrong model. The obvious mechanism is
measurement error, so reader precision was set from the published inter-observer
figure of about 10% on a single lesion diameter. That moved timepoint concordance
from 96.5% to 95.0% — almost nothing, because the response bands are wide enough
to absorb noise.

The mechanism that actually matters is **lesion selection**. RECIST allows at
most five target lesions and at most two per organ, and each reader selects their
own from whatever the subject has. A subject with eight measurable lesions gives
two readers real scope to follow different disease. With that modelled, 43% of
subjects have different target sets between the two readers, and the discordance
lands where real trials do.

`tumour.yaml` exposes `selection_size_preference`, and setting it to 1.0 makes
both readers rank purely by size and choose identically — which is the control
that shows the divergence is a consequence of that parameter rather than an
accident of the random seed. There is a test for it.

Readers also disagree about whether a new finding is malignant, via
`new_lesion_concurrence`. That judgement is taken **once per reader per subject**,
not per timepoint: rolling it at every assessment made progression flicker on and
off, which no radiologist would produce.

---

## How a site's character propagates

Sites reference a performance archetype rather than restating their behaviour,
because that is how a study manager thinks about a portfolio: a couple of strong
recruiters, one whose contract never gets signed, one whose data quality
generates three times the queries of anyone else.

The archetype then shows up in the data rather than as a flag on a row. Site
`DE-003` is `POOR_DATA_QUALITY`, and in the generated dataset it has:

- 3.2× the study mean query rate per form,
- a 15-day data entry lag against about a day at the best site,
- 1.29 protocol deviations per subject against about 0.4 elsewhere,
- and the study's **only** for-cause monitoring visit — triggered by its own
  query rate crossing a declared multiple of the study mean, not by being named.

Enrolment works the same way: it is derived from site activation, not configured.
A site cannot randomise anybody before its green light, and green light waits on
whichever of contract, ethics opinion and regulatory authorisation lands last.
Site `ES-002` is `SLOW_CONTRACT`; its contract takes five months, and its subject
count reflects the months it was closed rather than how well it recruits.

Trial master file completeness is an outcome too. An artifact becomes expected
when its milestone is reached — an executed contract is not expected before the
contract exists — and then arrives, arrives late, or never arrives. The result is
92% complete and 83% on time, and the gaps are nameable: site staff CVs,
financial disclosure forms, drug accountability logs. Which is precisely where a
real TMF loses its percentage.

---

## Safety, and why the grade matters

The adverse-event table is not the interesting part. What matters is that the
**grade drives the dose**: a Grade 3 non-haematological event interrupts dosing
and the subject resumes one level down, so a subject who has a bad time on
treatment receives less drug.

Because the active arm has more Grade 3 events, its relative dose intensity comes
out lower — 0.844 against 0.903 in the shipped dataset, from 71 Grade 3+ events
against 27, and 41 dose reductions against 9. Five subjects stopped treatment for
toxicity rather than progression, and that reaches the disposition form as
`DSREAS = ADVERSE EVENT`. None of those numbers is configured; they are what
`safety.yaml` and `dose_modification.yaml` produce.

Three details that a safety reviewer checks immediately:

**Seriousness is not severity.** A Grade 2 event requiring hospitalisation is
serious; a Grade 3 one managed at home is not. Seriousness is drawn against the
regulatory criteria — `AESERCRIT` names which one — with grade shifting the
probability rather than deciding it. Both directions appear in the data.

**Attribution is not arm.** Anaemia, neutropenia and alopecia come from the
carboplatin and pemetrexed that *both* arms receive, and appear at the same rate
in each. Diarrhoea and transaminase rises come from the KRAS inhibitor and are
three times higher on the active arm. A profile where every event is worse on one
arm has been scaled rather than modelled, and there is a test asserting the
backbone events match within 10 percentage points.

**Grade 5 is never drawn.** A fatal event belongs to the survival model, which
ties death to disease burden. Letting an adverse-event table kill subjects would
double count mortality and break that relationship — so the loader rejects a
Grade 5 entry in the grade weights outright, and a test confirms it.

Haematological toxicity is treated differently from everything else, because it
is expected: Grade 3 interrupts dosing but does not reduce it, and only Grade 4
brings the dose down. Reducing the investigational product for marrow suppression
the chemotherapy caused would be reducing the wrong drug.

The dose-modification rules are first-match, and the discontinuations are declared
before the interruptions. The linter checks that ordering, because a
discontinuation rule placed after a broader interruption rule can never fire —
subjects who should come off treatment would merely be reduced, and nothing
downstream would notice.

---

## The investigational product chain

Every dose resolves back to a manufactured batch:

```
dosing.kit_number -> imp_kits -> imp_lots -> batch_id -> the plant's batch_data
```

Thirteen integrity checks walk it, and `verify-spine` refuses to write a dataset
if any fails. They are ordered by consequence:

1. **A subject received the treatment they were randomised to.** Nothing else
   here matters if this fails, and it is not a hypothetical — dispensing from a
   shuffled shelf regardless of arm was the first implementation.
2. A kit is dispensed once.
3. Kit resolves to lot and batch.
4. Nothing dispensed before it arrived, or after its lot expired.
5. Nothing supplied from a shipment that failed its temperature check.
6. Every SDTM `EX` record reconciles to a dispensing record.
7. Expiry provenance is consistent — every lot's expiry came from the same kind
   of source.
8. Kit numbers reveal nothing.

That last one is a real mistake somebody has made. Kit numbers come from a single
pre-generated shuffled pool, so sorting `imp_kits.csv` by number does not
segregate the treatments. Assigning numbers at shipping time gave consecutive
numbers to consecutive kits of one treatment — a resupply shipment carries one
treatment only — and anybody holding the list could have reconstructed the
allocation.

---

## Reading the output

`rs.csv`, one row per assessment per reader:

| Column | Meaning |
|---|---|
| `RSSTRESC` | The response: `CR`, `PR`, `SD`, `PD`, `NE` |
| `RSEVALID` | **Which reader.** `INV` is the site investigator, `BICR` central review |
| `SUMDIAM` | The sum of diameters this response was derived from |
| `NADIR` | The smallest sum so far. Progression is judged against this |
| `PCHGBASE` / `PCHGNADIR` | Percent change from baseline / from the nadir |
| `RSTESTCD` | `OVRLRESP` per visit, `BESTRESP` for the subject's best overall |

`adtte.csv`, one row per subject per reader:

| Column | Meaning |
|---|---|
| `AVAL` | Days to progression, death or censoring |
| `CNSR` | 0 is an event, 1 is censored — the ADaM convention, and the opposite way round to how most people write it |
| `EVNTDESC` | The event, or the reason for censoring |

Every censoring reason is reproducible from the visit data. A subject censored
`MISSED_ASSESSMENTS_BEFORE_EVENT` had two consecutive missed scans before their
progression, so the date is unknowable and they are censored at their last
adequate assessment rather than counted as an event.

Join on `USUBJID` throughout, and `SITEID` for site. The EDC tables use
lower-case column names and the SDTM and ADaM tables use upper-case, which is the
convention in both worlds.

---

## Adding a study

1. Declare the arms in `protocol.yaml`, and give each one growth parameters under
   `growth.arms` in `tumour.yaml` — the linter rejects an arm without them,
   because a subject in an arm with no disease model cannot be simulated.
2. Map each arm to a product role in `config/lifecycle/links.yaml`. This is
   declared rather than inferred from the arm label: getting it wrong would give
   half the subjects the other arm's product and nothing downstream would notice.
3. Adjust `sites.yaml` for the countries and site mix. Enrolment follows.
4. The parts that need thought in `tumour.yaml`:
   - `sensitive_fraction`, `shrinkage_rate_per_week` and `growth_rate_per_week`
     per arm. Response rate and median PFS are *consequences* of these three, so
     check the resulting ORR before assuming it is what you wanted. A first
     attempt at the control arm had the resistant fraction outrunning the
     sensitive one from week zero, so it returned a 2.5% response rate — a
     control arm that cannot respond makes the whole comparison meaningless.
   - `measurable_lesion_count`. If most subjects have five or fewer lesions,
     both readers select all of them and reader discordance collapses.
5. `.venv/bin/python -c "from pharma_sim.clinical.loader import
   load_clinical_config; load_clinical_config('config/clinical')"` to lint before
   running anything.

---

## Limitations

Specific about what is not there:

- **No medical coding workflow.** A MedDRA subset is declared in `safety.yaml`
  with the hierarchy coding uses, and events carry `AEDECOD`, `AEPTCD`,
  `AEBODSYS` and `AESOCCD`. What is absent is the *process*: auto-coding hit
  rates, manual review, coder queries and a coding dictionary version upgrade.
  Events are emitted already coded. WHODrug and concomitant medications do not
  exist at all.
- **Adverse events do not affect the disease.** Toxicity shortens exposure, and
  reduced exposure ought to shorten response — but the lesion model is drawn
  independently of the dose received, so a subject who spent half the study
  interrupted responds as well as one who took every tablet. Closing that would
  make relative dose intensity a predictor of PFS, which is the relationship a
  pharmacometrician would look for first.
- **No safety database.** SAE reconciliation appears as a reconciliation row
  against an implied external system rather than a modelled one.
- **The imaging vendor is not an entity.** Central review happens, but the
  reading charter, the readers as people and the adjudication of discordant reads
  are not modelled. Adding them would strengthen the discordance story.
- **Screening is not modelled.** The plan describes a two-step molecular
  pre-screening funnel — roughly 1,000 consented to randomise 120, since KRAS
  G12C prevalence is about 13% — and enrolment currently starts at randomisation.
- **One study.** The Phase I dose escalation and the Phase III in the plan do not
  exist, so nothing exercises DLT windows, RP2D selection or a second protocol
  against the same substance.
- **No eCTD assembly.** SDTM and ADaM are emitted; define.xml, the reviewer's
  guides and the module structure are not.
- **The dataset is post-unblinding.** Subject arms and kit roles are both
  present, which is consistent, but there is no blinded view for anyone wanting
  to test a process that runs before unblinding.
