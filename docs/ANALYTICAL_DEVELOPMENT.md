# The analytical development domain

Generates a complete ICH Q2(R2) method validation — chromatograms, peaks, system
suitability, acceptance criteria and a Part 11 audit trail — for a fictional
oncology drug substance. This is the first vertical slice of the laboratory
domain described in [the lifecycle extension plan](LIFECYCLE_EXTENSION.md).

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

Add `--no-traces` to skip the chromatogram points, which are the bulk of it.

---

## Contents

- [The one idea that matters](#the-one-idea-that-matters)
- [What the dataset is of](#what-the-dataset-is-of)
- [The configuration files](#the-configuration-files)
- [How an injection happens](#how-an-injection-happens)
- [Where precision comes from](#where-precision-comes-from)
- [Reading the validation report](#reading-the-validation-report)
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
  are named in the plan and not built. The chiral method `MTH-0002` is declared
  and loadable but has no validation defined.
- **Peak purity is a proxy.** It is driven by the measured resolution of the
  critical pair rather than by a simulated PDA spectral comparison, which would
  need a spectral model per analyte.
- **No LIMS or stability yet.** Sample login, test plans, second-person result
  review, certificates of analysis, ICH Q1A stability pulls, shelf-life
  regression and OOS investigations are all planned and none exist. The formulation
  DoE is not built either, so nothing yet connects a method to a product.
- **Not linked to the plant.** The spine in
  [the plan](LIFECYCLE_EXTENSION.md#5-the-spine-one-identity-graph) requires that
  the validated method's precision bound the QC precision the factory sees.
  Nothing enforces that yet, because the manufacturing side has no
  `method_id` on its QC results.
- **Gradient methods are modelled as isocratic.** Retention responds to the
  organic percentage as a single number rather than to a gradient table, which is
  adequate for how conditions shift peaks but would not survive a conversation
  about gradient delay volume.
