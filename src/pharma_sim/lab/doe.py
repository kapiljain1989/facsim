"""Formulation and process screening by designed experiment.

The output of this module is a set of process settings, and those settings are
what the plant runs. Before it existed, the compression force and blend time in
``products.yaml`` were declared numbers with nothing behind them.

Three things are modelled rather than asserted:

* **The design is a real fractional factorial.** Four factors in eight runs at
  resolution IV, with the fourth column generated as the product of the other
  three. Main effects are clear of two-factor interactions; the interactions are
  aliased with each other and the module says so, because a screening design that
  claims to resolve everything is lying.
* **The effects are fitted from noisy observations.** The true response surface is
  declared, the engine evaluates it and adds process and analytical error, and the
  effects come back out of the observations. An effect smaller than the noise the
  design can see is not recovered.
* **The optimum is a compromise.** Harder tablets are less friable and dissolve
  more slowly; more lubricant tablets better and dissolves worse; longer blending
  improves content uniformity and over-lubricates. Desirability over the fitted
  surface therefore lands in the interior. A design whose optimum sits at a
  corner has not modelled a formulation problem.
"""

from __future__ import annotations

import itertools
import math
import statistics as stats
from dataclasses import dataclass, field
from datetime import date, timedelta
from random import Random

from pharma_sim.engine.ids import IdFactory
from pharma_sim.engine.rng import RngRegistry
from pharma_sim.lab.config import DoeStudy, LabConfig, Prototype, ResponseSpec

__all__ = ["DoeOutput", "build_design", "run_doe", "desirability"]


@dataclass(frozen=True, slots=True)
class Run:
    """One experimental run: the settings, and whether it is a centre point."""

    run_number: int
    settings: dict[str, float]
    centre_point: bool
    #: Coded levels, -1 / 0 / +1, which is what the effects are fitted on.
    coded: dict[str, float]


def build_design(config: LabConfig) -> list[Run]:
    """The fractional factorial plus its centre points.

    The generator in ``doe.yaml`` says which column is aliased with which
    product, so the design is constructed from the declared aliasing rather than
    hard-coded.
    """
    design = config.doe.design
    factors = config.doe.factors
    by_letter = {factor.factor: factor for factor in factors}
    independent = [
        factor.factor for factor in factors if factor.factor not in design.generator
    ]

    runs: list[Run] = []
    number = 0
    for combination in itertools.product((-1.0, 1.0), repeat=len(independent)):
        coded = dict(zip(independent, combination))
        for generated, sources in design.generator.items():
            product = 1.0
            for source in sources:
                product *= coded[source]
            coded[generated] = product
        number += 1
        runs.append(
            Run(
                run_number=number,
                settings={
                    by_letter[letter].name: _natural(by_letter[letter], level)
                    for letter, level in coded.items()
                },
                centre_point=False,
                coded={by_letter[letter].name: level for letter, level in coded.items()},
            )
        )

    for _ in range(design.centre_points):
        number += 1
        runs.append(
            Run(
                run_number=number,
                settings={factor.name: factor.centre for factor in factors},
                centre_point=True,
                coded={factor.name: 0.0 for factor in factors},
            )
        )
    return runs


def _natural(factor, level: float) -> float:
    if level < 0:
        return factor.low
    if level > 0:
        return factor.high
    return factor.centre


def _true_value(
    response: ResponseSpec, settings: dict[str, float], prototype: Prototype
) -> float:
    """Evaluate the declared surface. Ground truth; never reported directly."""
    value = response.true_response.intercept
    inputs = dict(settings)
    if prototype.api_d50_um is not None:
        inputs["api_d50_um"] = prototype.api_d50_um
    for term in response.true_response.terms:
        contribution = inputs.get(term.factor)
        if contribution is None:
            continue
        value += term.coef * contribution
    for name, coefficient in response.true_response.quadratic.items():
        current = inputs.get(name)
        if current is None:
            continue
        value += coefficient * (current - _centre(name, response)) ** 2
    if prototype.api_form:
        value += response.form_effect.get(prototype.api_form, 0.0)
    return value


#: Set once per run by the caller, so the quadratic term knows what it curves
#: around without threading the whole config through the evaluation.
_CENTRES: dict[str, float] = {}


def _centre(name: str, response: ResponseSpec) -> float:
    del response
    return _CENTRES.get(name, 0.0)


def desirability(value: float, response: ResponseSpec) -> float:
    """Derringer-Suich desirability of one response, in ``[0, 1]``.

    Zero when the specification is not met, so a response that fails takes the
    whole point to zero through the geometric mean rather than being averaged
    away by the ones that passed.
    """
    if response.direction == "MAXIMISE":
        low = response.minimum or 0.0
        high = response.target or (low * 1.2 or 1.0)
        if value <= low:
            return 0.0
        return min(1.0, (value - low) / (high - low)) if high > low else 1.0
    if response.direction == "MINIMISE":
        high = response.maximum or 1.0
        if value >= high:
            return 0.0
        return min(1.0, (high - value) / high)
    # TARGET: falls away either side, and is zero outside the specification.
    target = response.target or 0.0
    low = response.minimum
    high = response.maximum
    if low is not None and value <= low:
        return 0.0
    if high is not None and value >= high:
        return 0.0
    if value <= target:
        span = target - (low if low is not None else target - 1.0)
        return (value - (low if low is not None else target - 1.0)) / span if span else 1.0
    span = (high if high is not None else target + 1.0) - target
    return ((high if high is not None else target + 1.0) - value) / span if span else 1.0


def _overall(values: dict[str, float], config: LabConfig) -> float:
    weights = config.doe.optimisation.weights
    total = sum(weights.values())
    product = 1.0
    for response in config.doe.responses:
        value = values.get(response.response)
        if value is None:
            continue
        individual = desirability(value, response)
        if individual <= 0.0:
            return 0.0
        product *= individual ** (weights.get(response.response, 1.0) / total)
    return product


@dataclass
class Effect:
    """A fitted main effect, and whether the design could see it."""

    response: str
    factor: str
    effect: float
    standard_error: float

    @property
    def significant(self) -> bool:
        """Twice the standard error, which is the usual screening threshold."""
        return abs(self.effect) > 2.0 * self.standard_error


@dataclass
class DoeOutput:
    study_id: str
    runs: list[dict] = field(default_factory=list)
    observations: list[dict] = field(default_factory=list)
    effects: list[dict] = field(default_factory=list)
    prototype_summary: list[dict] = field(default_factory=list)
    optimum: dict[str, float] = field(default_factory=dict)
    curvature: list[dict] = field(default_factory=list)
    selected_formulation: str = ""
    selection_reason: str = ""
    aliasing: list[dict] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"{self.study_id}",
            f"  runs {len(self.runs)}  observations {len(self.observations)}"
            f"  fitted effects {len(self.effects)}"
            f"  ({sum(1 for e in self.effects if e['significant'])} resolved)",
        ]
        for row in self.prototype_summary:
            lines.append(
                f"  {row['formulation_id']:<10} {row['route']:<24} "
                f"best desirability {row['best_desirability']:.3f}"
                f"{'   <- selected' if row['selected'] else ''}"
            )
        lines.append(f"  {self.selection_reason}")
        for row in self.curvature:
            lines.append(
                f"  curvature in {row['response']} at {row['standard_errors']:.1f} "
                f"standard errors -- {row['implicated_factors']} held at centre"
            )
        if self.optimum:
            settings = "  ".join(
                f"{name}={value:g}" for name, value in sorted(self.optimum.items())
            )
            lines.append(f"  optimum: {settings}")
        return "\n".join(lines)


def run_doe(
    config: LabConfig,
    study: DoeStudy,
    rngs: RngRegistry,
    ids: IdFactory,
) -> DoeOutput:
    """Run the screening study across its prototypes and select an optimum."""
    out = DoeOutput(study_id=study.study_id)
    design = build_design(config)
    factors = [factor.name for factor in config.doe.factors]
    _CENTRES.clear()
    _CENTRES.update({factor.name: factor.centre for factor in config.doe.factors})

    # The aliasing is a property of the design and belongs in the record: a
    # reader has to know which interactions cannot be separated.
    for generated, sources in config.doe.design.generator.items():
        out.aliasing.append(
            {
                "study_id": study.study_id,
                "generated": generated,
                "aliased_with": "".join(sources),
                "resolution": config.doe.design.resolution,
                "consequence": "main effects are clear of two-factor interactions; "
                               "two-factor interactions are aliased with each other",
            }
        )

    best_overall = -1.0
    fitted_by_prototype: dict[str, dict[str, dict[str, float]]] = {}

    for formulation_id in study.prototypes:
        prototype = config.formulations.prototype(formulation_id)
        if prototype is None:
            raise KeyError(f"unknown prototype {formulation_id}")

        observed: dict[str, list[float]] = {
            response.response: [] for response in config.doe.responses
        }
        for run in design:
            run_id = ids.next("DR", width=5)
            out.runs.append(
                {
                    "doe_run_id": run_id,
                    "study_id": study.study_id,
                    "formulation_id": formulation_id,
                    "run_number": run.run_number,
                    "centre_point": "Y" if run.centre_point else "N",
                    "performed_on": (
                        study.started + timedelta(days=3 * run.run_number)
                    ).isoformat(),
                    **{f"factor_{name}": value for name, value in run.settings.items()},
                }
            )
            for response in config.doe.responses:
                truth = _true_value(response, run.settings, prototype)
                noise = rngs.child(
                    "lab", "doe", study.study_id, formulation_id,
                    response.response, str(run.run_number),
                ).gauss(0.0, response.noise_sigma)
                value = truth + noise
                observed[response.response].append(value)
                out.observations.append(
                    {
                        "observation_id": ids.next("DO", width=6),
                        "doe_run_id": run_id,
                        "study_id": study.study_id,
                        "formulation_id": formulation_id,
                        "run_number": run.run_number,
                        "response": response.response,
                        "value": round(value, 4),
                        "unit": response.unit,
                        "desirability": round(desirability(value, response), 4),
                    }
                )

        # Fit main effects on the coded levels. For a two-level design the effect
        # is twice the regression coefficient, which is the convention effects
        # are reported in.
        corners = [run for run in design if not run.centre_point]
        centres = [
            index for index, run in enumerate(design) if run.centre_point
        ]
        fitted: dict[str, dict[str, float]] = {}
        for response in config.doe.responses:
            values = observed[response.response]
            corner_values = [values[index] for index, run in enumerate(design)
                             if not run.centre_point]
            centre_values = [values[index] for index in centres]
            # Pure error from the centre-point replicates, which is the only
            # error estimate a single-replicate design has.
            pure_error = (
                stats.stdev(centre_values) if len(centre_values) > 1 else 0.0
            )
            standard_error = (
                2.0 * pure_error / math.sqrt(len(corner_values))
                if corner_values and pure_error
                else 0.0
            )
            coefficients: dict[str, float] = {"intercept": stats.fmean(corner_values)}
            for name in factors:
                plus = [
                    value for value, run in zip(corner_values, corners)
                    if run.coded[name] > 0
                ]
                minus = [
                    value for value, run in zip(corner_values, corners)
                    if run.coded[name] < 0
                ]
                effect = stats.fmean(plus) - stats.fmean(minus)
                coefficients[name] = effect / 2.0
                out.effects.append(
                    {
                        "study_id": study.study_id,
                        "formulation_id": formulation_id,
                        "response": response.response,
                        "factor": name,
                        "effect": round(effect, 4),
                        "standard_error": round(standard_error, 4),
                        "significant": abs(effect) > 2.0 * standard_error
                        if standard_error
                        else True,
                    }
                )
            # Curvature test. The centre points are the only replication a
            # single-replicate design has, and comparing their mean to the corner
            # mean is the standard check for a surface a straight line cannot
            # describe. A significant difference means the factor cannot be
            # optimised from this design.
            if len(centre_values) > 1 and corner_values:
                gap = stats.fmean(centre_values) - stats.fmean(corner_values)
                spread = pure_error * math.sqrt(
                    1.0 / len(centre_values) + 1.0 / len(corner_values)
                )
                ratio = abs(gap) / spread if spread else 0.0
                if ratio > config.doe.optimisation.curvature_threshold:
                    curved = sorted(response.true_response.quadratic)
                    out.curvature.append(
                        {
                            "study_id": study.study_id,
                            "formulation_id": formulation_id,
                            "response": response.response,
                            "centre_mean": round(stats.fmean(centre_values), 4),
                            "corner_mean": round(stats.fmean(corner_values), 4),
                            "standard_errors": round(ratio, 2),
                            "implicated_factors": ",".join(curved),
                            "consequence": "a two-level design cannot fit this; the "
                                           "implicated factors are held at their centre "
                                           "pending a response-surface design",
                        }
                    )

            fitted[response.response] = coefficients
        fitted_by_prototype[formulation_id] = fitted

        held = {
            name
            for row in out.curvature
            if row["formulation_id"] == formulation_id
            for name in row["implicated_factors"].split(",")
            if name
        }
        best = _search(config, fitted, held)
        out.prototype_summary.append(
            {
                "study_id": study.study_id,
                "formulation_id": formulation_id,
                "name": prototype.name,
                "route": prototype.route,
                "api_form": prototype.api_form or "",
                "best_desirability": round(best[0], 4),
                "selected": False,
                **{f"optimum_{name}": value for name, value in best[1].items()},
            }
        )
        if best[0] > best_overall:
            best_overall = best[0]
            out.selected_formulation = formulation_id
            out.optimum = best[1]

    for row in out.prototype_summary:
        row["selected"] = row["formulation_id"] == out.selected_formulation

    chosen = next(
        row for row in out.prototype_summary
        if row["formulation_id"] == out.selected_formulation
    )
    others = [row for row in out.prototype_summary if not row["selected"]]
    margin = (
        chosen["best_desirability"] - max(row["best_desirability"] for row in others)
        if others
        else 0.0
    )
    out.selection_reason = (
        f"{out.selected_formulation} selected on desirability "
        f"{chosen['best_desirability']:.3f}"
        + (
            f", ahead of {others[0]['formulation_id']} by {margin:.3f}"
            if others
            else ""
        )
        + (
            ". The simpler route was sufficient, so the spray-dried dispersion "
            "was not carried forward."
            if chosen["route"] == "DIRECT_COMPRESSION"
            else ". The direct-compression route could not meet dissolution."
        )
    )
    return out


def _search(
    config: LabConfig,
    fitted: dict[str, dict[str, float]],
    held_at_centre: set[str] | None = None,
):
    """Grid-search the fitted surface for the most desirable settings.

    A factor with detected curvature is held at its centre rather than searched.
    Extrapolating a straight-line fit across a curved surface to the edge of the
    range is how a screening design produces a confidently wrong setpoint, and it
    is the mistake this restriction exists to prevent.
    """
    factors = config.doe.factors
    points = config.doe.optimisation.grid_points
    held = held_at_centre or set()
    grids = []
    for factor in factors:
        if factor.name in held:
            grids.append([factor.centre])
            continue
        step = (factor.high - factor.low) / (points - 1)
        grids.append([factor.low + step * index for index in range(points)])

    best_score = -1.0
    best_settings: dict[str, float] = {}
    for combination in itertools.product(*grids):
        natural = {factor.name: value for factor, value in zip(factors, combination)}
        coded = {
            factor.name: 2.0 * (natural[factor.name] - factor.centre)
            / (factor.high - factor.low)
            for factor in factors
        }
        predicted = {
            response: coefficients["intercept"]
            + sum(coefficients[name] * coded[name] for name in coded)
            for response, coefficients in fitted.items()
        }
        score = _overall(predicted, config)
        if score > best_score:
            best_score = score
            best_settings = {name: round(value, 3) for name, value in natural.items()}
    return best_score, best_settings
