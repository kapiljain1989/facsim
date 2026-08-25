"""Formulation and process screening by designed experiment.

Three claims. The design is a real fractional factorial with real aliasing; the
effects are fitted from noisy observations rather than read back from the surface
that produced them; and the optimum is a compromise between responses that
genuinely conflict.

The last one is what the tests here mostly defend, because a design whose optimum
sits at a corner has not modelled a formulation problem — it has modelled a
factor that only helps or only hurts.
"""

from __future__ import annotations

import collections
import statistics as stats
from pathlib import Path

import pytest

from pharma_sim.engine.ids import IdFactory
from pharma_sim.engine.rng import RngRegistry
from pharma_sim.lab.doe import build_design, desirability, run_doe
from pharma_sim.lab.loader import load_lab_config

CONFIG = Path(__file__).resolve().parents[1] / "config" / "lab"


@pytest.fixture(scope="module")
def config():
    return load_lab_config(CONFIG)


@pytest.fixture(scope="module")
def executed(config):
    return run_doe(config, config.doe.studies[0], RngRegistry(42), IdFactory())


class TestTheDesign:
    def test_run_count_matches_the_fraction(self, config):
        design = config.doe.design
        runs = build_design(config)
        corners = 2 ** (design.factors - design.fraction)
        assert len(runs) == corners + design.centre_points

    def test_the_generated_column_is_the_product_of_its_sources(self, config):
        """This is what makes it resolution IV rather than a random subset."""
        by_letter = {factor.factor: factor.name for factor in config.doe.factors}
        for run in build_design(config):
            if run.centre_point:
                continue
            for generated, sources in config.doe.design.generator.items():
                product = 1.0
                for source in sources:
                    product *= run.coded[by_letter[source]]
                assert run.coded[by_letter[generated]] == pytest.approx(product)

    def test_every_factor_is_balanced(self, config):
        """Each factor sits high as often as low, or the effects are confounded
        with the mean."""
        corners = [run for run in build_design(config) if not run.centre_point]
        for name in (factor.name for factor in config.doe.factors):
            assert sum(run.coded[name] for run in corners) == pytest.approx(0.0)

    def test_factors_are_pairwise_orthogonal(self, config):
        corners = [run for run in build_design(config) if not run.centre_point]
        names = [factor.name for factor in config.doe.factors]
        for i, first in enumerate(names):
            for second in names[i + 1 :]:
                dot = sum(run.coded[first] * run.coded[second] for run in corners)
                assert dot == pytest.approx(0.0), f"{first} and {second} are confounded"

    def test_centre_points_sit_at_the_centre(self, config):
        centres = {factor.name: factor.centre for factor in config.doe.factors}
        for run in build_design(config):
            if run.centre_point:
                assert run.settings == centres

    def test_the_aliasing_is_recorded(self, executed):
        """A screening design that does not say what it cannot separate is
        claiming more than it knows."""
        assert executed.aliasing
        for row in executed.aliasing:
            assert row["aliased_with"]
            assert "aliased" in row["consequence"]


class TestDesirability:
    def test_a_failed_specification_scores_zero(self, config):
        dissolution = next(
            r for r in config.doe.responses if r.response == "dissolution_30min_percent"
        )
        assert desirability(70.0, dissolution) == 0.0

    def test_meeting_the_target_scores_one(self, config):
        dissolution = next(
            r for r in config.doe.responses if r.response == "dissolution_30min_percent"
        )
        assert desirability(95.0, dissolution) == pytest.approx(1.0)

    def test_a_target_response_falls_away_on_both_sides(self, config):
        hardness = next(
            r for r in config.doe.responses if r.response == "tablet_hardness_n"
        )
        assert desirability(95.0, hardness) > desirability(85.0, hardness)
        assert desirability(95.0, hardness) > desirability(110.0, hardness)
        assert desirability(60.0, hardness) == 0.0
        assert desirability(130.0, hardness) == 0.0

    def test_a_minimise_response_prefers_less(self, config):
        friability = next(
            r for r in config.doe.responses if r.response == "friability_percent"
        )
        assert desirability(0.2, friability) > desirability(0.8, friability)
        assert desirability(1.5, friability) == 0.0


class TestTheOptimumIsACompromise:
    def test_no_factor_optimises_to_a_corner(self, config):
        """The property that distinguishes a modelled formulation problem from a
        set of one-directional effects.

        Compression force trades hardness and friability against dissolution and
        ejection force; lubricant trades ejection force against everything else;
        disintegrant trades dissolution against hardness. Blend time is held at
        its centre because the design detects curvature it cannot fit.
        """
        out = run_doe(config, config.doe.studies[0], RngRegistry(3), IdFactory())
        factors = {factor.name: factor for factor in config.doe.factors}
        at_corner = [
            name
            for name, value in out.optimum.items()
            if abs(value - factors[name].low) < 1e-6
        ]
        assert not at_corner, f"{at_corner} optimised to the bottom of the range"

    def test_lubricant_is_not_minimised_away(self, config):
        """Every response except ejection force prefers less lubricant. Without
        ejection force the optimum runs to the bottom and the tablet sticks to the
        tooling."""
        out = run_doe(config, config.doe.studies[0], RngRegistry(7), IdFactory())
        lubricant = next(f for f in config.doe.factors if f.name == "lubricant_percent")
        assert out.optimum["lubricant_percent"] > lubricant.low

    def test_the_selected_route_is_the_simpler_one(self, config):
        """The spray-dried dispersion dissolves better and costs a spray dryer and
        a physical-stability risk. It should only win if direct compression cannot
        meet dissolution, and here it can."""
        selected = set()
        for seed in range(1, 13):
            out = run_doe(config, config.doe.studies[0], RngRegistry(seed), IdFactory())
            selected.add(out.selected_formulation)
        assert selected == {"FRM-0001"}, f"selection is not robust: {selected}"

    def test_the_optimum_is_stable_across_studies(self, config):
        """Fitted from noisy runs, so it moves — but not by much, or the setpoint
        it produces would be meaningless."""
        values = []
        for seed in range(1, 13):
            out = run_doe(config, config.doe.studies[0], RngRegistry(seed), IdFactory())
            values.append(out.optimum["main_compression_force"])
        assert max(values) - min(values) <= 1.5, values


class TestCurvature:
    def test_it_is_detected(self, executed):
        assert executed.curvature
        assert all(row["standard_errors"] > 2.0 for row in executed.curvature)

    def test_it_is_detected_every_time(self, config):
        """Marginal detection was a real bug: blend time was held at its centre on
        some seeds and extrapolated to the edge of the range on others, so the
        setpoint it produced flipped between 24 and 28."""
        for seed in range(1, 16):
            out = run_doe(config, config.doe.studies[0], RngRegistry(seed), IdFactory())
            assert out.curvature, f"no curvature detected at seed {seed}"

    def test_a_curved_factor_is_held_at_its_centre(self, config):
        centre = next(f for f in config.doe.factors if f.name == "blend_time").centre
        for seed in range(1, 16):
            out = run_doe(config, config.doe.studies[0], RngRegistry(seed), IdFactory())
            implicated = {
                name
                for row in out.curvature
                for name in row["implicated_factors"].split(",")
                if name
            }
            if "blend_time" in implicated:
                assert out.optimum["blend_time"] == pytest.approx(centre)


class TestEffectsAreFitted:
    def test_the_known_direction_of_each_main_effect_is_recovered(self, executed):
        """Signs, not magnitudes: the noise is real, so an effect near the noise
        floor legitimately comes back wrong in size."""
        expected = {
            ("tablet_hardness_n", "main_compression_force"): 1,
            ("tablet_hardness_n", "lubricant_percent"): -1,
            ("friability_percent", "main_compression_force"): -1,
            ("dissolution_30min_percent", "main_compression_force"): -1,
            ("dissolution_30min_percent", "disintegrant_percent"): 1,
            ("ejection_force_n", "lubricant_percent"): -1,
        }
        found = {
            (row["response"], row["factor"]): row["effect"]
            for row in executed.effects
            if row["formulation_id"] == "FRM-0001"
        }
        for key, sign in expected.items():
            assert key in found, key
            assert found[key] * sign > 0, f"{key} came back with the wrong sign"

    def test_an_effect_below_the_noise_is_not_claimed(self, executed):
        """A screening design resolves what it can resolve. If everything were
        flagged significant the standard error would not be being used."""
        flags = [row["significant"] for row in executed.effects]
        assert any(flags) and not all(flags)

    def test_every_response_is_observed_in_every_run(self, executed, config):
        per_run = collections.Counter(
            (row["formulation_id"], row["run_number"]) for row in executed.observations
        )
        assert set(per_run.values()) == {len(config.doe.responses)}


class TestReproducibility:
    def test_same_seed_agrees(self, config):
        def run():
            out = run_doe(config, config.doe.studies[0], RngRegistry(11), IdFactory())
            return out.optimum, [row["value"] for row in out.observations]

        assert run() == run()

    def test_a_different_seed_moves_the_observations(self, config):
        def observations(seed):
            out = run_doe(config, config.doe.studies[0], RngRegistry(seed), IdFactory())
            return [row["value"] for row in out.observations]

        assert observations(1) != observations(2)
