"""API and dashboard.

Two properties matter most here and both are asserted:

* **Read-only.** There is no route that mutates anything. A dashboard with no
  write path has no auth surface to get wrong, and the test enforces that rather
  than trusting it.
* **No leakage.** No operational endpoint may serve a ground-truth or label
  field. The dashboard reads the same data an analyst would, answers included
  nowhere.
"""

from __future__ import annotations

import json

import pytest

fastapi = pytest.importorskip("fastapi", reason="API extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

from pharma_sim.api.live import HubSink, LiveHub  # noqa: E402
from pharma_sim.api.main import AppSettings, create_app  # noqa: E402
from pharma_sim.api.service import DataService  # noqa: E402


@pytest.fixture(scope="module")
def populated(tmp_path_factory):
    """A small dataset on disk for the API to read."""
    import shutil

    from pharma_sim.config.loader import load_config
    from pharma_sim.config.models import (
        EvaluationStorage,
        StorageConfig,
        TimeseriesStorage,
        TransactionalStorage,
    )
    from pharma_sim.simulator import Simulator
    from tests.conftest import CONFIG_DIR, _apply_test_profile

    root = tmp_path_factory.mktemp("api")
    config_dir = root / "config"
    shutil.copytree(CONFIG_DIR, config_dir)

    # create_app() loads configuration from disk, so the file has to name the
    # temp paths — patching the object here would leave the server reading the
    # repository's own data directory.
    storage_file = config_dir / "storage.yaml"
    storage_file.write_text(
        storage_file.read_text()
        .replace("./data/factory.db", str(root / "factory.db"))
        .replace("./data/telemetry", str(root / "telemetry"))
        .replace("./data/eval", str(root / "eval"))
    )
    storage = StorageConfig(
        transactional=TransactionalStorage(backend="sqlite", dsn=str(root / "factory.db")),
        timeseries=TimeseriesStorage(
            backend="parquet",
            dsn=str(root / "telemetry"),
            partition_by=["date", "unit_id"],
        ),
        evaluation=EvaluationStorage(backend="parquet", dsn=str(root / "eval")),
    )
    config = load_config(config_dir)
    object.__setattr__(config, "storage", storage)
    _apply_test_profile(config)

    sim = Simulator(
        config_dir,
        config=config,
        seed=42,
        reset_storage=True,
        configure_logs=False,
        log_level="ERROR",
    )
    sim.run(days=2)
    sim.storage.flush()
    sim.close()
    return config_dir, storage


@pytest.fixture(scope="module")
def client(populated):
    config_dir, _ = populated
    app = create_app(AppSettings(config_dir=str(config_dir), live=False))
    with TestClient(app) as test_client:
        yield test_client


class TestReadOnly:
    def test_no_route_mutates(self, client):
        """The whole point of "no auth": there is nothing to protect."""
        schema = client.get("/openapi.json").json()
        mutating = []
        for path, operations in schema["paths"].items():
            for method in operations:
                if method.lower() in {"post", "put", "patch", "delete"}:
                    mutating.append(f"{method.upper()} {path}")
        assert not mutating, f"unexpected write endpoints: {mutating}"

    def test_write_methods_are_rejected(self, client):
        for method, path in [
            ("post", "/api/plant"),
            ("delete", "/api/machines/TP-001"),
            ("put", "/api/batches"),
        ]:
            response = getattr(client, method)(path)
            assert response.status_code in (404, 405), (method, path, response.status_code)


class TestEndpoints:
    def test_health_and_status(self, client):
        assert client.get("/api/health").json()["status"] == "ok"
        status = client.get("/api/status").json()
        assert status["live"] is False
        assert status["tables"]["machines"] > 0

    @pytest.mark.parametrize(
        "path",
        [
            "/api/plant",
            "/api/units",
            "/api/machines",
            "/api/batches",
            "/api/qc",
            "/api/qc/by-parameter",
            "/api/deviations",
            "/api/rca",
            "/api/capa",
            "/api/failures",
            "/api/failures/by-category",
            "/api/maintenance",
            "/api/employees",
            "/api/shifts",
            "/api/employee-events",
            "/api/events",
            "/api/trends/production",
            "/api/trends/oee",
            "/api/integrity",
        ],
    )
    def test_endpoint_returns_data(self, client, path):
        response = client.get(path)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body or body == [], path

    def test_topology_matches_the_configuration(self, client, populated):
        from pharma_sim.config.loader import load_config

        config_dir, _ = populated
        config = load_config(config_dir)
        assert len(client.get("/api/units").json()) == len(config.units.units)
        declared = sum(
            g.count for groups in config.machines.layout.values() for g in groups
        )
        assert len(client.get("/api/machines").json()) == declared

    def test_machine_detail_and_series(self, client):
        machines = client.get("/api/machines").json()
        machine_id = next(m["machine_id"] for m in machines if m["sensor_count"] > 4)
        detail = client.get(f"/api/machines/{machine_id}").json()
        assert detail["sensors"]
        assert detail["state_history"]

        tag = next(s["tag"] for s in detail["sensors"] if not s["derived_from"])
        series = client.get(
            f"/api/machines/{machine_id}/sensors/{tag}/series?hours=6"
        ).json()
        assert series["points"], "no telemetry read back"
        for point in series["points"][:20]:
            assert set(point) == {"t", "v", "q"}
            assert point["q"] in {"GOOD", "UNCERTAIN", "BAD"}

    def test_series_is_ordered_in_time(self, client):
        machines = client.get("/api/machines").json()
        machine_id = machines[0]["machine_id"]
        detail = client.get(f"/api/machines/{machine_id}").json()
        tag = next(s["tag"] for s in detail["sensors"] if not s["derived_from"])
        points = client.get(
            f"/api/machines/{machine_id}/sensors/{tag}/series?hours=24"
        ).json()["points"]
        stamps = [p["t"] for p in points]
        assert stamps == sorted(stamps)

    def test_batch_detail_assembles_the_genealogy(self, client):
        batches = client.get("/api/batches?limit=100").json()
        completed = [b for b in batches if b["completed_at"]]
        assert completed, "no completed batches"
        batch = client.get(f"/api/batches/{completed[0]['batch_id']}").json()
        assert batch["stages"]
        assert "qc_results" in batch
        for stage in batch["stages"]:
            assert stage["machine_id"]
        timeline = client.get(f"/api/batches/{batch['batch_id']}/timeline").json()
        assert timeline
        assert [e["timestamp"] for e in timeline] == sorted(
            e["timestamp"] for e in timeline
        )

    def test_unknown_ids_are_404(self, client):
        assert client.get("/api/machines/NOT_A_MACHINE").status_code == 404
        assert client.get("/api/batches/NOT_A_BATCH").status_code == 404
        assert client.get("/api/rca/NOT_AN_RCA").status_code == 404

    def test_query_bounds_are_enforced(self, client):
        assert client.get("/api/events?limit=999999").status_code == 422
        assert (
            client.get("/api/machines/TP-001/sensors/vibration/series?hours=-1").status_code
            == 422
        )

    def test_integrity_endpoint_reports_checks(self, client):
        report = client.get("/api/integrity").json()
        assert report["checks"]
        assert report["ok"], report["checks"]

    def test_filters_narrow_results(self, client):
        machines = client.get("/api/machines?unit_id=UNIT-06").json()
        assert machines
        assert all(m["unit_id"] == "UNIT-06" for m in machines)
        failing = client.get("/api/qc?result=PASS&limit=20").json()
        assert all(r["result"] == "PASS" for r in failing)


class TestNoLeakage:
    """§25 again, at the API boundary this time."""

    LABEL_FIELDS = {
        "root_cause_description",
        "rul_hours",
        "will_fail_24h",
        "will_fail_72h",
        "will_fail_168h",
        "failure_mode",
        "degradation_stage",
        "scheduled_fault_at",
        "onset_at",
    }

    @pytest.mark.parametrize(
        "path",
        ["/api/machines", "/api/failures", "/api/batches", "/api/qc", "/api/events"],
    )
    def test_operational_endpoints_do_not_serve_labels(self, client, path):
        body = client.get(path).json()
        rows = body if isinstance(body, list) else [body]
        for row in rows[:50]:
            leaked = self.LABEL_FIELDS & set(row)
            assert not leaked, f"{path} exposes {leaked}"

    def test_no_endpoint_serves_ground_truth(self, client):
        schema = client.get("/openapi.json").json()
        for path in schema["paths"]:
            assert "ground" not in path.lower()
            assert "label" not in path.lower()

    def test_rca_serves_a_conclusion_not_the_truth(self, client):
        """An RCA root cause is a claim the investigation made; that is allowed."""
        reports = client.get("/api/rca").json()
        if not reports:
            pytest.skip("no investigations in this window")
        report = reports[0]
        assert "root_cause" in report          # the claim
        assert "confidence" in report          # and how sure it was
        assert "failure_mode" not in report    # never the hidden mode


class TestDashboardAssets:
    def test_index_is_served(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "Pharmaceutical Factory Simulator" in response.text

    @pytest.mark.parametrize("asset", ["app.js", "charts.js", "styles.css"])
    def test_static_assets_are_served(self, client, asset):
        response = client.get(f"/static/{asset}")
        assert response.status_code == 200
        assert len(response.text) > 500

    def test_charts_module_caps_series_at_the_validated_count(self, client):
        """The palette validates three slots all-pairs; the code must not cycle."""
        source = client.get("/static/charts.js").text
        assert "MAX_SERIES" in source
        assert source.count("var(--series-") >= 3
        assert "var(--series-4" not in source

    def test_no_external_asset_references(self, client):
        """The dashboard must work offline: no CDN, no remote fonts."""
        for asset in ("/", "/static/app.js", "/static/charts.js", "/static/styles.css"):
            text = client.get(asset).text
            assert "http://" not in text.replace("http://www.w3.org", "")
            assert "https://" not in text


class TestLiveHub:
    def test_publish_reaches_a_subscriber(self):
        import asyncio

        async def scenario():
            hub = LiveHub()
            hub.bind(asyncio.get_running_loop())
            subscriber = hub.subscribe()
            HubSink(hub).write([{"kind": "telemetry", "value": 1.0}])
            await asyncio.sleep(0)  # let call_soon_threadsafe run
            message = await asyncio.wait_for(subscriber.queue.get(), timeout=2)
            assert message["value"] == 1.0
            hub.unsubscribe(subscriber)
            assert hub.subscriber_count == 0

        asyncio.run(scenario())

    def test_replay_buffer_serves_late_joiners(self):
        hub = LiveHub()
        HubSink(hub).write([{"kind": "telemetry", "n": index} for index in range(50)])
        replay = hub.replay(10)
        assert len(replay) == 10
        assert replay[-1]["n"] == 49

    def test_slow_subscriber_drops_oldest_and_counts_it(self):
        """A browser that stops reading must not stall the simulation."""
        import asyncio

        async def scenario():
            hub = LiveHub(max_queue=5)
            hub.bind(asyncio.get_running_loop())
            subscriber = hub.subscribe()
            sink = HubSink(hub)
            for index in range(40):
                sink.write([{"kind": "telemetry", "n": index}])
                await asyncio.sleep(0)
            assert subscriber.queue.qsize() <= 5
            assert hub.dropped > 0
            assert sink.stats().dropped == hub.dropped

        asyncio.run(scenario())

    def test_publishing_without_a_loop_is_safe(self):
        """Messages produced before a browser connects must not raise."""
        hub = LiveHub()
        HubSink(hub).write([{"kind": "event", "event_type": "X"}])
        assert hub.published == 1
        assert hub.dropped == 0

    def test_subscribers_are_distinguished_by_identity(self):
        import asyncio

        async def scenario():
            hub = LiveHub()
            hub.bind(asyncio.get_running_loop())
            first, second = hub.subscribe(), hub.subscribe()
            assert hub.subscriber_count == 2
            hub.unsubscribe(first)
            assert hub.subscriber_count == 1
            hub.unsubscribe(second)

        asyncio.run(scenario())


class TestLiveMode:
    """The embedded plant. Slow, so one short run covers the path."""

    @pytest.mark.slow
    def test_live_server_streams_over_the_websocket(self, tmp_path):
        import shutil

        from tests.conftest import CONFIG_DIR

        config_dir = tmp_path / "config"
        shutil.copytree(CONFIG_DIR, config_dir)
        # Keep the embedded run's data out of the repository's data directory.
        storage = config_dir / "storage.yaml"
        storage.write_text(
            storage.read_text()
            .replace("./data/factory.db", str(tmp_path / "factory.db"))
            .replace("./data/telemetry", str(tmp_path / "telemetry"))
            .replace("./data/eval", str(tmp_path / "eval"))
        )

        app = create_app(
            AppSettings(
                config_dir=str(config_dir),
                live=True,
                seed=42,
                speed=6000.0,
                warmup_hours=1.0,
            )
        )
        with TestClient(app) as client:
            assert client.get("/api/health").json()["live"] is True
            status = client.get("/api/status").json()
            assert status["live"] is True
            assert status["error"] is None
            # A live run must keep going, not finish after the warm-up.
            assert status["running"] is True

            with client.websocket_connect("/ws/live") as socket:
                frame = json.loads(socket.receive_text())
                assert frame["type"] in {"replay", "batch"}
                assert frame["messages"]
                kinds = {m.get("kind") for m in frame["messages"]}
                assert "telemetry" in kinds

            # Regression: /api/machines used to derive "state" from the last row
            # of machine_state_history, an append-only log of *closed* intervals.
            # That log never contains the state a machine is currently sitting
            # in — only the one it just left — so any machine settled into a
            # long-running state indefinitely showed the state before it. The
            # live app must read the in-memory plant instead.
            plant = client.app.state.app_state.live.simulator.plant
            rows = {row["machine_id"]: row for row in client.get("/api/machines").json()}
            assert rows
            for machine_id, machine in plant.machines.items():
                assert rows[machine_id]["state"] == machine.state


class TestDataService:
    def test_sensor_series_downsamples_but_keeps_the_last_point(self, populated):
        from pharma_sim.config.loader import load_config
        from pharma_sim.storage.factory import build_storage

        config_dir, storage_config = populated
        config = load_config(config_dir)
        object.__setattr__(config, "storage", storage_config)
        storage = build_storage(config.storage, reset=False)
        storage.initialise()
        try:
            service = DataService(storage, config)
            machine_id = service.machines()[0]["machine_id"]
            tag = next(
                s["tag"] for s in service.machine_sensors(machine_id) if not s["derived_from"]
            )
            dense = service.sensor_series(machine_id, tag, hours=48, limit=10_000)
            sparse = service.sensor_series(machine_id, tag, hours=48, limit=25)
            assert len(sparse["points"]) <= 26
            if dense["points"]:
                assert sparse["points"][-1]["t"] == dense["points"][-1]["t"]
        finally:
            storage.close()
