"""Chromatogram synthesis and peak integration.

The claim these tests defend is that the peak table is *measured* from the trace
rather than declared: synthesis is given true peak parameters, integration never
sees them, and the descriptors it recovers have to agree with the analytic truth.

Tolerances are empirical, set from observed accuracy with a little headroom.
Where a measurement is biased rather than noisy the bias is asserted with its
sign and reason, because a bias that silently reversed would be a real defect.
"""

from __future__ import annotations

import math
from random import Random

import pytest

from pharma_sim.lab.chromatography import (
    IntegratedPeak,
    PeakSpec,
    TraceSpec,
    emg,
    erfcx,
    gaussian_half_width,
    integrate,
    robust_noise_sigma,
    synthesise,
)

SIGMA = 0.06
RT = 8.5
AREA = 50_000.0


def _trace(peaks, *, noise=0.0, drift=0.0, wander=0.0, hz=5.0, minutes=15.0, seed=1):
    spec = TraceSpec(
        run_time_min=minutes,
        sampling_hz=hz,
        noise_sigma=noise,
        baseline_offset=2.0,
        baseline_drift_per_min=drift,
        baseline_wander=wander,
    )
    return synthesise(peaks, spec, Random(seed))


class TestErfcx:
    """The scaled complementary error function, which the EMG depends on."""

    def test_known_values(self):
        assert erfcx(0.0) == pytest.approx(1.0, abs=1e-12)
        assert erfcx(1.0) == pytest.approx(math.exp(1.0) * math.erfc(1.0), rel=1e-12)

    def test_the_two_branches_agree_where_they_meet(self):
        """A mismatch here would put a step in every peak tail.

        Compared at the same argument, not at neighbouring ones: erfcx has a real
        slope, so neighbouring values differ legitimately.
        """
        switch = 20.0
        asymptotic = erfcx(switch)
        direct = math.exp(switch * switch) * math.erfc(switch)
        assert asymptotic == pytest.approx(direct, rel=1e-9)

    def test_is_monotonically_decreasing_through_the_switch(self):
        values = [erfcx(19.99), erfcx(20.0), erfcx(20.01)]
        assert values[0] > values[1] > values[2]

    def test_large_argument_does_not_overflow(self):
        """exp(z**2) alone would overflow above z ~ 26."""
        assert erfcx(170.0) == pytest.approx(1.0 / (170.0 * math.sqrt(math.pi)), rel=1e-4)


class TestEmg:
    def test_converges_to_a_gaussian_as_tau_vanishes(self):
        exact = AREA / (SIGMA * math.sqrt(2.0 * math.pi))
        assert emg(RT, RT, SIGMA, 1e-9, AREA) == pytest.approx(exact, rel=1e-9)

    @pytest.mark.parametrize("tau", [1e-6, 0.01, 0.05, 0.15])
    def test_area_is_preserved_across_tailing(self, tau):
        """Response must stay proportional to concentration whatever the shape."""
        low, high = RT - 20 * SIGMA, RT + 40 * SIGMA + 60 * tau
        steps = 60_000
        step = (high - low) / steps
        total = sum(emg(low + i * step, RT, SIGMA, tau, AREA) for i in range(steps + 1)) * step
        assert total == pytest.approx(AREA, rel=2e-4)

    def test_far_from_the_peak_is_zero_not_nan(self):
        assert emg(0.0, 12.0, 0.05, 0.03, AREA) == 0.0

    def test_rejects_nonpositive_sigma(self):
        with pytest.raises(ValueError):
            emg(1.0, 1.0, 0.0, 0.01, 1.0)


class TestIntegrationOfASinglePeak:
    @pytest.fixture(scope="class")
    @staticmethod
    def symmetric() -> IntegratedPeak:
        times, response = _trace([PeakSpec("main", RT, SIGMA, 1e-6, AREA)])
        found = integrate(times, response)
        assert len(found) == 1
        return found[0]

    def test_recovers_area(self, symmetric):
        assert symmetric.area == pytest.approx(AREA, rel=1e-3)

    def test_recovers_retention_time(self, symmetric):
        assert symmetric.retention_time_min == pytest.approx(RT, abs=0.01)

    def test_symmetric_peak_has_unit_tailing(self, symmetric):
        assert symmetric.tailing_usp == pytest.approx(1.0, abs=0.01)

    def test_half_height_width_matches_the_gaussian_identity(self, symmetric):
        assert symmetric.width_half_min == pytest.approx(gaussian_half_width(SIGMA), rel=5e-3)

    def test_plate_count_matches_the_analytic_value(self, symmetric):
        """For a Gaussian, 5.54*(tR/W50)**2 reduces to (tR/sigma)**2."""
        assert symmetric.plate_count_usp == pytest.approx((RT / SIGMA) ** 2, rel=1e-2)

    def test_tailing_is_reported_above_one_for_a_tailed_peak(self):
        times, response = _trace([PeakSpec("main", RT, SIGMA, 0.05, AREA)])
        peak = integrate(times, response)[0]
        assert peak.tailing_usp > 1.10
        assert peak.area == pytest.approx(AREA, rel=5e-3)


class TestResolution:
    """Measured on a resolution solution, which is what USP <621> intends."""

    @pytest.mark.parametrize(
        "separation,analytic",
        [(0.50, 2.09), (0.40, 1.67), (0.30, 1.25), (0.25, 1.04)],
    )
    def test_tracks_the_analytic_value(self, separation, analytic):
        peaks = [
            PeakSpec("imp", RT - separation, SIGMA, 0.03, 40_000.0),
            PeakSpec("main", RT, SIGMA, 0.03, AREA),
        ]
        times, response = _trace(peaks, noise=2.0, drift=0.3)
        found = integrate(times, response)
        assert len(found) == 2
        measured = found[1].resolution_previous
        assert measured is not None
        assert measured == pytest.approx(analytic, abs=0.20)

    def test_measurement_is_biased_low_on_overlapping_peaks(self):
        """Overlap broadens each peak's measured W50, which deflates R.

        Asserted with its sign: a high bias would mean the integrator was
        reporting a cleaner separation than the trace actually contains, which is
        the direction that would hide a failing method.
        """
        peaks = [
            PeakSpec("imp", RT - 0.30, SIGMA, 0.03, 40_000.0),
            PeakSpec("main", RT, SIGMA, 0.03, AREA),
        ]
        times, response = _trace(peaks, noise=2.0)
        measured = integrate(times, response)[1].resolution_previous
        analytic = 1.18 * 0.30 / (2 * gaussian_half_width(SIGMA))
        assert measured is not None
        assert measured < analytic

    def test_an_unresolved_pair_is_reported_as_one_peak(self):
        """No valley means no split. Reporting two areas here would be a lie."""
        peaks = [
            PeakSpec("imp", RT - 0.10, SIGMA, 0.03, 1_000.0),
            PeakSpec("main", RT, SIGMA, 0.03, AREA),
        ]
        times, response = _trace(peaks, noise=2.0)
        found = integrate(times, response)
        assert len(found) == 1
        assert found[0].area == pytest.approx(AREA + 1_000.0, rel=1e-2)


class TestRealisticRun:
    """Six analytes on a drifting, wandering, noisy baseline."""

    PEAKS = (
        PeakSpec("imp_a", 4.18, 0.050, 0.020, 900.0),
        PeakSpec("imp_b", 5.83, 0.055, 0.025, 450.0),
        PeakSpec("imp_c", 7.95, 0.060, 0.030, 700.0),
        PeakSpec("main", 8.52, 0.060, 0.030, AREA),
        PeakSpec("near_loq", 10.50, 0.060, 0.030, 55.0),
        PeakSpec("imp_d", 12.40, 0.080, 0.050, 300.0),
    )

    @pytest.fixture(scope="class")
    @staticmethod
    def found():
        times, response = _trace(TestRealisticRun.PEAKS, noise=3.0, drift=0.4, wander=2.0)
        return integrate(times, response)

    def test_finds_every_analyte(self, found):
        assert len(found) == len(self.PEAKS)

    def test_retention_times_are_in_order_and_close(self, found):
        for peak, truth in zip(found, self.PEAKS):
            assert peak.retention_time_min == pytest.approx(truth.retention_time_min, abs=0.05)

    def test_main_peak_area_survives_the_baseline(self, found):
        main = max(found, key=lambda peak: peak.area)
        assert main.area == pytest.approx(AREA, rel=5e-3)

    def test_signal_to_noise_orders_with_height(self, found):
        ratios = [peak.signal_to_noise for peak in found]
        assert all(ratio is not None and ratio > 0 for ratio in ratios)
        main = max(found, key=lambda peak: peak.area)
        loq = min(found, key=lambda peak: peak.area)
        assert main.signal_to_noise > loq.signal_to_noise

    def test_the_near_loq_peak_loses_tail_area(self, found):
        """Real behaviour near the limit of quantitation, not a defect.

        Integration cannot recover the part of the tail that the noise swallows,
        so recovery falls short. It must fall *short*, never over.
        """
        loq = min(found, key=lambda peak: peak.area)
        assert 0.80 < loq.area / 55.0 < 1.0


class TestNoiseEstimation:
    def test_recovers_the_injected_noise_despite_peaks(self):
        times, response = _trace(TestRealisticRun.PEAKS, noise=3.0, drift=0.4)
        assert robust_noise_sigma(response) == pytest.approx(3.0, rel=0.08)

    def test_reports_near_zero_for_a_clean_trace(self):
        times, response = _trace([PeakSpec("main", RT, SIGMA, 0.02, AREA)])
        assert robust_noise_sigma(response) < 1.0


class TestReproducibility:
    def test_same_seed_gives_an_identical_trace(self):
        first = _trace(TestRealisticRun.PEAKS, noise=3.0, seed=7)[1]
        second = _trace(TestRealisticRun.PEAKS, noise=3.0, seed=7)[1]
        assert first == second

    def test_different_seed_gives_a_different_trace(self):
        first = _trace(TestRealisticRun.PEAKS, noise=3.0, seed=7)[1]
        second = _trace(TestRealisticRun.PEAKS, noise=3.0, seed=8)[1]
        assert first != second


class TestInvariantsFromRealBugs:
    """Each of these failed at some point during development.

    They are the properties that matter most for a chromatography dataset, and
    all four were violated by code that looked reasonable.
    """

    @pytest.mark.parametrize(
        "separation", [0.80, 0.60, 0.50, 0.40, 0.30, 0.25, 0.22, 0.20, 0.15, 0.10, 0.05, 0.02]
    )
    def test_area_is_conserved_at_every_degree_of_overlap(self, separation):
        """No area invented, and very little lost, however fused the pair is.

        Two earlier versions failed this. One dropped a badly fused doublet
        entirely, because the valley sat above half height and both halves failed
        the width test — the region's whole area vanished. The next reported the
        measurable half and silently discarded the other, which is worse: it
        looks like a clean result and surfaces later as an unexplained mass
        balance failure.
        """
        truth = 40_000.0 + 50_000.0
        peaks = [
            PeakSpec("imp", RT - separation, SIGMA, 0.03, 40_000.0),
            PeakSpec("main", RT, SIGMA, 0.03, 50_000.0),
        ]
        times, response = _trace(peaks, noise=2.0, drift=0.3)
        found = integrate(times, response)
        assert found, "a region above the threshold must yield at least one peak"
        recovered = sum(peak.area for peak in found) / truth
        assert 0.90 <= recovered <= 1.001, f"recovered {recovered:.3%}"

    def test_detection_does_not_scale_with_the_tallest_peak(self):
        """A 0.05% impurity beside a 100% main peak must still be found.

        The threshold once had a floor proportional to the trace's span, which
        looks harmless until the trace has real dynamic range. At 2000:1 it hid
        every impurity the method exists to measure.
        """
        main_area = 50_000.0
        impurity_area = main_area * 0.0005
        peaks = [
            PeakSpec("imp", 6.00, SIGMA, 0.03, impurity_area),
            PeakSpec("main", RT, SIGMA, 0.03, main_area),
        ]
        times, response = _trace(peaks, noise=2.0, drift=0.3)
        found = integrate(times, response)
        assert len(found) == 2
        small = min(found, key=lambda peak: peak.area)
        assert small.area == pytest.approx(impurity_area, rel=0.25)

    def test_no_phantom_peaks_on_the_flanks_of_a_tall_one(self):
        """Noise on a peak's flank produces local maxima above any absolute
        threshold. Only a prominence test rejects them."""
        times, response = _trace([PeakSpec("main", RT, SIGMA, 0.03, 500_000.0)], noise=6.5)
        found = integrate(times, response)
        assert len(found) == 1

    def test_no_peak_is_reported_twice(self):
        """Widening two adjacent regions can make them overlap; without merging
        them, an apex inside both is integrated twice."""
        peaks = (
            PeakSpec("a", 4.18, 0.050, 0.020, 900.0),
            PeakSpec("b", 4.55, 0.052, 0.022, 700.0),
            PeakSpec("c", 8.52, 0.060, 0.030, 50_000.0),
            PeakSpec("d", 12.40, 0.080, 0.050, 300.0),
        )
        times, response = _trace(peaks, noise=6.5, drift=11.0, wander=24.0)
        found = integrate(times, response)
        retentions = [round(peak.retention_time_min, 4) for peak in found]
        assert len(retentions) == len(set(retentions)), f"duplicates in {retentions}"
