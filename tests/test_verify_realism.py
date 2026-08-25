"""The realism gate.

Referential integrity says every row resolves; this says the numbers are the
right numbers. The tests that matter are the ones proving it fails — a gate that
cannot fail is worse than no gate, because it reports success.
"""

from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_realism.py"
CONFIG = ROOT / "config" / "realism.yaml"


@pytest.fixture(scope="module")
def module():
    spec = importlib.util.spec_from_file_location("verify_realism", SCRIPT)
    loaded = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(loaded)
    return loaded


@pytest.fixture(scope="module")
def envelopes():
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


class TestTheConfiguration:
    def test_every_section_can_be_computed(self, module, envelopes):
        """Asserted against what the script knows how to compute rather than
        against a hard-coded list -- the list version failed the moment a section
        was added, which told nobody anything useful."""
        assert set(envelopes) == set(module._SECTIONS)
        assert envelopes, "no envelopes declared"

    def test_every_envelope_is_ordered_and_justified(self, envelopes):
        """An envelope nobody can justify is one somebody widens the first time it
        fails, so a reason is required rather than optional."""
        for section, expectations in envelopes.items():
            for expectation in expectations:
                name = expectation["metric"]
                assert expectation["low"] <= expectation["high"], f"{section}.{name}"
                reason = expectation.get("why", "")
                assert len(str(reason).split()) >= 8, (
                    f"{section}.{name} has no usable justification"
                )

    def test_metric_names_are_unique(self, envelopes):
        names = [e["metric"] for section in envelopes.values() for e in section]
        assert len(names) == len(set(names))

    def test_every_declared_metric_is_computed_somewhere(self, module, envelopes):
        """A metric the config asks for and the script cannot produce is reported
        as MISSING and fails the run, but catching it here is cheaper."""
        source = SCRIPT.read_text(encoding="utf-8")
        for section, expectations in envelopes.items():
            for expectation in expectations:
                assert f'"{expectation["metric"]}"' in source, expectation["metric"]


class TestStatistics:
    def test_spearman_on_a_perfect_ranking(self, module):
        pairs = [(float(i), float(i)) for i in range(20)]
        assert module._spearman(pairs) == pytest.approx(1.0)

    def test_spearman_on_a_reversed_ranking(self, module):
        pairs = [(float(i), float(-i)) for i in range(20)]
        assert module._spearman(pairs) == pytest.approx(-1.0)

    def test_spearman_needs_enough_points(self, module):
        assert module._spearman([(1.0, 1.0), (2.0, 2.0)]) is None

    def test_median_survival_ignores_censored_observations_as_events(self, module):
        times = [10.0, 20.0, 30.0, 40.0, 50.0]
        assert module._median_survival(times, [True] * 5) == 30.0

    def test_median_survival_is_none_when_not_reached(self, module):
        assert module._median_survival([10.0, 20.0], [False, False]) is None


class TestItActuallyFails:
    """Each of these writes a deliberately broken dataset and requires a failure."""

    @staticmethod
    def _dataset(directory: Path, *, invert_exposure: bool = False,
                 identical_readers: bool = False, break_spine: bool = False) -> Path:
        directory.mkdir(parents=True, exist_ok=True)

        subjects, exposure, rs, adtte, tu = [], [], [], [], []
        for index in range(80):
            subject = f"S-{index:03d}"
            subjects.append({"USUBJID": subject, "ARM": "ARM-A"})
            # Exposure spread across the range.
            intensity = 0.5 + 0.5 * (index / 79.0)
            exposure.append(
                {
                    "subject_id": subject,
                    "arm": "ARM-A",
                    "relative_dose_intensity": f"{intensity:.4f}",
                    "discontinued_for_toxicity": "N",
                }
            )
            # Deeper response with more exposure, unless we are inverting it.
            depth = -60.0 * intensity if not invert_exposure else -60.0 * (1.5 - intensity)
            rs.append(
                {
                    "USUBJID": subject, "VISITNUM": "2", "RSTESTCD": "OVRLRESP",
                    "RSEVALID": "BICR", "RSSTRESC": "PR",
                    "PCHGBASE": f"{depth:.2f}", "SUMDIAM": "50", "NADIR": "50",
                }
            )
            rs.append(
                {
                    "USUBJID": subject, "VISITNUM": "2", "RSTESTCD": "OVRLRESP",
                    "RSEVALID": "INV",
                    "RSSTRESC": "PR" if identical_readers else ("SD" if index % 3 else "PR"),
                    "PCHGBASE": f"{depth:.2f}", "SUMDIAM": "50", "NADIR": "50",
                }
            )
            rs.append(
                {
                    "USUBJID": subject, "VISITNUM": "999", "RSTESTCD": "BESTRESP",
                    "RSEVALID": "BICR", "RSSTRESC": "PR" if index % 2 else "SD",
                    "PCHGBASE": "", "SUMDIAM": "", "NADIR": "",
                }
            )
            adtte.append(
                {
                    "USUBJID": subject, "PARAMCD": "PFS", "EVAL": "BICR",
                    "AVAL": f"{200 + index}", "CNSR": "0",
                }
            )
            for reader in ("INV", "BICR"):
                lesion = "L1" if (identical_readers or reader == "INV") else "L2"
                tu.append(
                    {
                        "USUBJID": subject, "TUEVALID": reader,
                        "TUORRES": "TARGET", "TULNKID": lesion,
                    }
                )

        def write(name: str, rows: list[dict]) -> None:
            path = directory / name
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)

        write("subjects.csv", subjects)
        write("exposure.csv", exposure)
        write("rs.csv", rs)
        write("adtte.csv", adtte)
        write("tu.csv", tu)
        write("dosing.csv", [
            {"kit_number": "K1" if not break_spine else "MISSING", "batch_id": "B1"}
        ])
        write("imp_kits.csv", [{"kit_number": "K1", "lot_id": "L1", "batch_id": "B1"}])
        write("imp_lots.csv", [{"lot_id": "L1", "batch_id": "B1"}])
        (directory / "manifest.json").write_text(
            json.dumps({"spine_checks": {"passed": 12 if break_spine else 13, "total": 13}})
        )
        return directory

    def test_a_healthy_synthetic_dataset_passes_the_metrics_it_covers(
        self, module, tmp_path
    ):
        root = self._dataset(tmp_path / "ok")
        metrics = module.clinical_metrics(root)
        assert metrics["exposure_response_rho_arm_a"] < -0.15
        assert metrics["reader_discordance"] > 0.03
        assert module.spine_metrics(root)["spine_checks_passed"] == 1.0

    def test_an_inverted_exposure_response_is_caught(self, module, tmp_path):
        """The regression this envelope was written for. Less drug giving a deeper
        response must not pass."""
        root = self._dataset(tmp_path / "inverted", invert_exposure=True)
        rho = module.clinical_metrics(root)["exposure_response_rho_arm_a"]
        assert rho > 0.0, "the fixture should invert the relationship"
        bound = self._bound("clinical", "exposure_response_rho_arm_a")
        assert not (bound[0] <= rho <= bound[1]), (
            "an inverted exposure-response relationship passed the envelope"
        )

    def test_readers_that_never_disagree_are_caught(self, module, tmp_path):
        root = self._dataset(tmp_path / "same", identical_readers=True)
        metrics = module.clinical_metrics(root)
        low, high = self._bound("clinical", "reader_discordance")
        assert not (low <= metrics["reader_discordance"] <= high)
        low, high = self._bound("clinical", "different_target_selection")
        assert not (low <= metrics["different_target_selection"] <= high)

    def test_a_broken_spine_is_caught(self, module, tmp_path):
        root = self._dataset(tmp_path / "broken", break_spine=True)
        metrics = module.spine_metrics(root)
        for name in ("spine_checks_passed", "doses_traced_to_batches"):
            low, high = self._bound("spine", name)
            assert not (low <= metrics[name] <= high), name

    @staticmethod
    def _bound(section: str, metric: str) -> tuple[float, float]:
        config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        for expectation in config[section]:
            if expectation["metric"] == metric:
                return float(expectation["low"]), float(expectation["high"])
        raise AssertionError(f"{section}.{metric} is not declared")


class TestAMissingMetricIsAFailure:
    def test_an_empty_directory_computes_nothing(self, module, tmp_path):
        """And the runner treats that as a failure rather than a skip, because a
        check that silently stopped running draws no attention to itself."""
        assert module.clinical_metrics(tmp_path) == {}
        assert module.manufacturing_metrics(tmp_path) == {}
        assert module.laboratory_metrics(tmp_path) == {}
