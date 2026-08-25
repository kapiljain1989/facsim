# The analytical development domain

Generates a complete ICH Q2(R2) method validation and an ICH Q1A stability
programme — chromatograms, peaks, system suitability, acceptance criteria, a
Part 11 audit trail, and a fitted shelf life — for a fictional oncology drug
substance. Part of the laboratory domain described in
[the lifecycle extension plan](LIFECYCLE_EXTENSION.md).

```bash
.venv/bin/python scripts/generate_lab_dataset.py --output data/lab
```

About thirty seconds, and you get:

| | |
|---|---|
| `sequences.csv` | 17 — one per experiment or robustness condition |
| `injections.csv` | 243, each with the conditions it ran under |
| `peaks.csv` | 586 integrated peaks with USP <621> descriptors |
| `system_suitability.csv` | 22 evaluated sets, including failed attempts |
| `validation_results.csv` | 49 metrics, each with its criterion and verdict |
| `audit_trail.csv` | 550 events |
| `chromatogram_points.parquet` | 1,093,743 points — 4,501 per injection |
| `stability_samples.csv` | 45 — 3 batches across 3 conditions and their timepoints |
| `stability_results.csv` | 225 measured attributes |
| `stability_trend.csv` | 225 trend points, per batch, condition and timepoint |
| `stability_shelf_life.csv` | 3 fitted shelf lives, one per attribute with a limit |
| `stability_injections.csv` | 180 — every timepoint injected, with its bracketing standard |
| `stability_oos.csv` | 6 investigations |
| `stability_certificates.csv`, `stability_reviews.csv` | 45 each |

Add `--no-traces` to skip the chromatogram points, which are the bulk of it.

A `README.md` is written into the output directory alongside the data: a short,
plain-language dataset card for whoever receives the files without this
repository. Every figure in it — row counts, the analyte count, the injection its
example plots — is derived from the run that produced it, so it cannot drift away
from the data it describes.

---

## Contents

- [The one idea that matters](#the-one-idea-that-matters)
- [What the dataset is of](#what-the-dataset-is-of)
- [The configuration files](#the-configuration-files)
- [How an injection happens](#how-an-injection-happens)
- [Where precision comes from](#where-precision-comes-from)
- [Reading the validation report](#reading-the-validation-report)
- [The designed experiment, and the setpoints it chose](#the-designed-experiment-and-the-setpoints-it-chose)
- [Stability, and a shelf life that is fitted](#stability-and-a-shelf-life-that-is-fitted)
- [Adding a method](#adding-a-method)
- [Limitations](#limitations)

---

## The one idea that matters

**Nothing in the output is declared. Everything is measured.**

`methods.yaml` holds the *truth* about each analyte — where it elutes, how wide
it is, how much it tails, how much signal it gives per unit concentration, and
how each of those responds to flow, temperature, organic content and pH.
[`method.py`](../src/pharma_sim/lab/method.py) turns that truth into a digitised
detector signal. [`chromatography.py`](../src/pharma_sim/lab/chromatography.py)
then reads peaks back **out** of that signal with no access to the truth that
made it.

So a peak area is what an integration algorithm found, not what the config said.
That distinction is the difference between a dataset that survives analysis and
one that falls apart the moment somebody recomputes something:

- A method whose true critical-pair resolution is 2.1 reports values scattered
  around 2.1, and reports below 2.0 when conditions drift.
- A peak near the limit of quantitation loses tail area to the noise, so recovery
  is 94% at 0.05% and 99% at 1% — and it loses area rather than gaining it.
- An unresolved pair is reported as **one** peak carrying the combined area,
  because that is what the trace contains.
- Mass balance closes, because area is conserved through integration.

The robustness study is the clearest case. Nothing anywhere says "organic
content is this method's weak point". What the config says is that
`des-fluoro Nelvorasib` has an `organic_percent` coefficient of `-0.0400` while
the main peak's is `-0.0550`. Raise organic by 2% and the two converge; the
resolution *measured from the trace* falls from 2.19 to 1.13; the suitability set
fails. The finding is a consequence of the configuration, discovered by running
the study.

---

## What the dataset is of

**Nelvorasib** (`NVR-101`) — an oral covalent KRAS G12C inhibitor. Fictional, but
the name follows the WHO INN stem `-rasib` used for this class, so it reads
correctly without colliding with a real product. No real patient data, study
identifier or company name appears anywhere; every number is generated.

The molecule is chosen to make the chemistry interesting rather than convenient:

| Property | Consequence in the data |
|---|---|
| Acrylamide warhead (what makes it covalent) | hydrolysis gives a specific degradant, promoted by the base stress condition |
| Hindered biaryl axis — **atropisomerism** | a second method (`MTH-0002`) on a chiral column exists to control it |
| Weak base, BCS class II, pH-dependent solubility | pH shifts both retention and tailing |
| Palladium from cross-coupling, secondary amine | ICH Q3D and nitrosamine methods belong in the panel |
| OEB 4, highly potent | the drug product needs a containment suite, not the existing tablet lines |

Five analytes on the assay method: the drug substance, two degradants (warhead
hydrolysis, N-oxide), one process impurity (des-fluoro) and one thermal dimer.
The des-fluoro impurity elutes 0.57 min before the main peak and is the
**critical pair** — the closest pair in the chromatogram, and the one system
suitability is built around.

---

## The configuration files

Five files under `config/lab/`. No Python holds a substance name, an analyte, a
suitability criterion or an acceptance limit.

| File | Declares |
|---|---|
| `substances.yaml` | The drug substance, its related substances and the excipients. Molecular properties, pH-solubility profile, polymorphs, stereochemistry, degradation pathways |
| `methods.yaml` | Two methods. Per analyte: true retention, width, tailing, response factor, specification limit, and sensitivity to each operating condition. Per method: detector noise and baseline behaviour, variability sources, column ageing |
| `instruments.yaml` | Three HPLC systems with their CDS binding, qualification and calibration state, and their own bias and precision. Columns, analysts and reference standards |
| `cds.yaml` | System suitability criteria per method, the resolution solution, the audit-trail event vocabulary and the rates for manual integration, reprocessing and aborted runs |
| `validation.yaml` | The ICH Q2(R2) protocol: nine experiments, their levels and replicates, and every acceptance criterion |

Cross-file references are plain strings by design, so
[`loader.py`](../src/pharma_sim/lab/loader.py) lints them: an analyte that is not
a declared substance, a suitability criterion naming a peak the method does not
have, a robustness factor that is not a chromatographic condition, a validation
pointing at an instrument nobody declared. It caught two mistakes in the shipped
config while that config was being written.

---

## How an injection happens

```
methods.yaml declares the truth
        |
        |  retention x condition effects x column ageing x injection jitter
        |  width    x retention scaling x efficiency effects x ageing
        |  tailing  x pH effect x ageing
        |  area     = concentration x response factor x composed bias
        v
   PeakSpec per analyte              <-- ground truth, never exported
        |
        |  summed as exponentially modified Gaussians onto a baseline that
        |  drifts and wanders, plus detector noise
        v
   4,501 digitised points
        |
        |  integrate(): robust noise estimate -> morphological baseline for
        |  region detection -> straight-line baseline drop per region ->
        |  perpendicular drop at valleys -> USP <621> descriptors
        v
   peak table: retention, area, height, W50, tailing, plate count,
               resolution, signal-to-noise
        |
        |  matched to expected retention UNDER THESE CONDITIONS, best gap
        |  first. A robustness condition moves retention by 10%, so matching
        |  against the nominal time would fail exactly when it matters.
        v
   labelled peaks -> assay, impurity percentages, suitability metrics
```

The integrator is deliberately unaware of the truth. It also has to behave when
the trace is difficult, and the awkward cases are the ones that were hardest to
get right:

- Noise on the flank of a tall peak makes local maxima that clear any absolute
  height threshold. Only a **prominence** test rejects them, and it has to be
  applied while merging apices — reject afterwards and a fused doublet loses both
  halves and the region's area vanishes.
- The detection threshold must come from the noise and never from the tallest
  peak. A related substances method puts a 0.05% impurity beside a 100% main
  peak; anything span-relative hides it.
- Baselines anchored on one point at each end put the full detector noise into
  every area. Averaging a short window is worth a factor of three in precision at
  the quantitation limit.

---

## Where precision comes from

Repeatability and intermediate precision are different numbers because the model
composes bias from sources with different scopes:

| Source | Shared by |
|---|---|
| sample preparation | every injection of the same preparation |
| injection | one injection |
| analyst bias and precision | every injection that analyst performs |
| day | every injection on that day |
| instrument bias and precision | that instrument |
| calibration drift | grows with days since calibration |
| column ageing | grows with injections on that column |

Each draws from its own named RNG stream, so adding an instrument does not
perturb another instrument's history.

This is why the distinction between **five injections of one preparation** and
**six separate preparations** is load-bearing throughout the code. The first
measures the injector and is what system suitability does; the second measures
the method and is what repeatability does. Preparation error enters once in the
first case and six times in the second. Collapse the two and the whole precision
story becomes one number with noise on it.

Intermediate precision then changes analyst, day *and* instrument at once — three
independent bias terms move together, which is the point of the experiment.

---

## Reading the validation report

The shipped validation passes 39 of its 41 judged criteria. The two failures are
the interesting part, and they are the same finding twice:

```
robustness   SUITABILITY_PASSES[organic_percent +2]        FAIL
robustness   SUITABILITY_PASSES[column_temperature_c +5]   FAIL
```

Both are critical-pair resolution failures. Every other robustness condition
passes, including both flow-rate variations — flow shifts all retention times
together and leaves selectivity alone, so resolution is unaffected. A model that
failed flow as well would be moving peaks around with no chromatography behind
it.

Two things in the record are worth looking at, because neither was programmed
directly:

**A spurious failure recovers; a physical one does not.** `cds.yaml` sets a 6%
chance that a first suitability attempt fails for a reason the physics does not
model — a bubble, a bad vial, a column that had not finished equilibrating. Those
pass on the re-run. The two robustness conditions above fail all three permitted
attempts with resolution measurements agreeing to within 0.02, because the cause
is the conditions rather than chance. The audit trail shows the difference.

**The assay result survives the condition that breaks the method.** At +2%
organic the critical pair is gone, but the assay value moves by only 0.65% — well
inside its 2.0% limit. The number looks fine while the method has stopped being
able to see the impurity next to the main peak. That is exactly why system
suitability is a release criterion and not a nicety.

The limit of quantitation is reported the way a report defends it. Two accepted
definitions disagree — the regression estimate `10σ/S` gives 0.149 µg/mL, and
interpolating to signal-to-noise 10 gives 0.287 µg/mL. The more conservative is
reported, and performance is then confirmed *at* that level rather than below it.

---

## The designed experiment, and the setpoints it chose

The plant's compression force and blend time used to be numbers in
`products.yaml` with nothing behind them. They are now the output of a screening
study, and `verify_realism.py` checks that manufacturing is running what
development selected — otherwise the experiment is decoration.

A fractional factorial: four factors in eight runs at resolution IV, with the
fourth column generated as the product of the other three, plus three centre
points. Main effects come out clear of two-factor interactions; the interactions
are aliased with each other and `doe_curvature.csv` and the aliasing record say
so, because a screening design that claims to resolve everything is lying.

**The optimum is a compromise, and getting that right took two rounds.** Harder
tablets are less friable and dissolve more slowly. More lubricant tablets better
and dissolves worse. More disintegrant dissolves faster and makes a softer tablet.
So the desirability optimum sits in the interior.

The first version did not. Three of four factors optimised to a corner, because
disintegrant only helped and lubricant only hurt — and a factor with no trade-off
has no interior optimum, so reporting a corner as "the optimum" is misleading.
The missing physics was ordinary formulation science: croscarmellose is a
disintegrant rather than a binder, so more of it costs hardness; and below about
0.5% magnesium stearate a direct-compression blend sticks to the tooling, so
there is an ejection-force response that gives lubricant a reason to exist.

**Blend time is held at its centre, and that is the honest answer.** Content
uniformity improves with blending and then gets worse again — continued blending
over-lubricates and a blend of unlike particle sizes segregates. A two-level
design **cannot fit** a curved factor; the centre points can only detect the
curvature. So the study reports curvature at four to six standard errors and
holds blend time at 24 minutes pending a response-surface design, rather than
extrapolating a straight-line fit to the edge of the range. That restriction was
also a bug once: at a weaker curvature the test sat marginally at its threshold,
so blend time was held on some seeds and extrapolated to 28 on others, and the
setpoint it produced flipped.

**The study moved the plant, in composition as well as process.** The optimum
sits at 12.7 kN across seeds, and `products.yaml` declared 11.5. Rather than tune the response surfaces until the
optimum matched the number already there — which would have been the wrong causal
direction and would have made the whole exercise decorative — the plant was
changed to 12.7 and the affected QC transfer intercept recalibrated with it. The
convention throughout is that nominal inputs land on target, so a setpoint change
means that intercept changes too.

The composition moved too. Disintegrant and lubricant are factors in the design,
so leaving the formulation at the levels it was first prototyped at would have
meant the plant building a tablet the study never evaluated — the selected
optimum would describe something nobody makes. Both propagate to
`formulations.yaml`, to the matching placebo, and to the plant's charge sheet,
and `doe_composition_agreement` checks it.

The direct-compression route wins on all twelve seeds tried. The spray-dried
dispersion dissolves better and costs a spray dryer, a physical-stability risk on
storage and a larger tablet, so it should only be selected if the simpler route
cannot meet dissolution — and here it can.

---

## Stability, and a shelf life that is fitted

Nothing declares a shelf life. Degradation runs on Arrhenius kinetics, the
samples are pulled on the ICH Q1A schedule, each pull is **injected on the assay
method** and read back off a synthesised chromatogram, and the shelf life is
where the ICH Q1E confidence bound on the limiting attribute meets its
specification. The answer is **27 months, limited by total impurities**, and it
changes if the activation energy does.

Three things come out of that rather than being arranged:

**The intermediate condition is tested because the data asked for it.** At
40 °C / 75% RH the acceleration factor is 7.4, and total impurities reach 1.47%
at six months against a 1.0% limit. That is significant change, which per ICH
Q1A(R2) is what triggers intermediate-condition testing — so the intermediate
condition is run rather than tested as a matter of course.

**Impurities are the limiting attribute, not assay.** This is the usual case and
worth reproducing: a tablet runs out of impurity headroom long before it runs out
of active. Assay barely moves — under 1% across the whole study.

**The impurity slope is recovered from the chromatograms.** The fitted slope comes
back at +0.0267 %/month against a declared 0.0267, entirely from integrated peak
areas. That is the check that the trend is measured rather than drawn.

Two details are worth knowing because they are easy to get wrong.

*Assay must be measured against a standard injected in the same sequence.*
Referenced instead to the method's nominal response factor, the assay inherited
the detector's response drift — +0.163 %/month, six times the true degradation
rate and the wrong sign, so the product appeared to *gain* active as it aged.
Impurities are reported as area percent of the main peak and were never affected,
and that asymmetry is what identified the cause. External standardisation exists
for exactly this.

*At three batches the assay trend is not resolvable.* ICH asks for three primary
batches. At three, the scatter on the assay is comparable to the total change
across the study, so the slope estimate is dominated by noise — which is stated
as a property with its own test, because it is the reason impurities set the shelf
life. A separate test at twelve batches confirms the degradation is really there.

The shelf life then dates the clinical lots. `imp_lots.csv` in the clinical
dataset records `expiry_source` as `STABILITY` when it came from this regression
and `DECLARED` when it fell back to a configured constant — those are different
claims and the data distinguishes them.

---

## Adding a method

1. Add any new analyte to `substances.yaml` first — the linter will reject a
   method referencing a substance that does not exist.
2. Add the method to `methods.yaml`. The parts that need thought:
   - `retention_time_min`, `sigma_min`, `tau_min` per analyte. Plate count comes
     out as roughly `(retention / sigma) ** 2`, so pick sigma to land where the
     column should be. `tau_min` around half of sigma gives a tailing factor near
     1.05.
   - `retention_sensitivity`. **Selectivity is the differences between analytes,
     not the absolute values.** Give two analytes the same coefficients and no
     condition will ever change their separation.
   - `detector.noise_sigma` sets the quantitation limit. For an impurity at
     `x`% of the standard to reach signal-to-noise 10, noise needs to be near
     `2 * height / 10` where `height ≈ area / (sigma * 2.5066)`.
3. Declare its suitability criteria and resolution solution in `cds.yaml`.
4. Write a validation for it in `validation.yaml`.
5. `.venv/bin/python -c "from pharma_sim.lab.loader import load_lab_config;
   load_lab_config('config/lab')"` to lint before running anything.

---

## Limitations

Honest about what this slice does not do yet:

- **One technique.** Dissolution, Karl Fischer water content, residual solvents
  by headspace GC, elemental impurities by ICP-MS and nitrosamines by LC-MS/MS
  are named in the plan and not built as methods. Dissolution and water content
  appear as stability attributes with their own drift, but they are modelled
  directly rather than measured from an instrument. The chiral method `MTH-0002`
  is declared and loadable but has no validation defined.
- **Peak purity is a proxy.** It is driven by the measured resolution of the
  critical pair rather than by a simulated PDA spectral comparison, which would
  need a spectral model per analyte.
- **LIMS records live inside the stability module.** Sample login, tests,
  second-person review, certificates of analysis and OOS investigations are
  emitted, but from `stability.py`, because it is currently their only consumer.
  When release testing uses the same lifecycle they should move to their own
  module rather than be duplicated. There is no LIMS around routine release
  testing today.
- **The screening design cannot optimise a curved factor.** It detects curvature
  and stops, which is correct, but it means blend time is carried at its centre
  rather than optimised. A central composite or Box-Behnken follow-up is the
  right next study and does not exist.
- **Interactions are aliased and stay that way.** Resolution IV keeps main
  effects clean but leaves two-factor interactions confounded with each other. No
  fold-over or follow-up design resolves them, so an interaction that mattered
  would be invisible.
- **The disintegrant answer is pinned to the edge of the range.** The study
  selects 8.0%, which is the top of what it explored, so its real conclusion is
  "at least 8%" rather than an optimum found in the interior. Widening the range
  is the obvious follow-up and has not been done.
- **The OOS investigation is a template.** Phase I and Phase II conclusions are
  recorded, but every investigation resolves the same way — confirmed
  product-related. A real programme has laboratory errors, invalidated assays,
  retests and resamples, and none of those outcomes occur here.
- **Gradient methods are modelled as isocratic.** Retention responds to the
  organic percentage as a single number rather than to a gradient table, which is
  adequate for how conditions shift peaks but would not survive a conversation
  about gradient delay volume.
