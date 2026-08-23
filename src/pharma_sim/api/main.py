"""FastAPI application: read-only REST endpoints plus a live WebSocket feed.

There is deliberately **no authentication and no write path**. Every route is a
query; nothing here can modify the factory, the configuration or the stored data.
That is a design constraint, not an omission — it keeps the surface small enough
to reason about, and it is what the dashboard needs and nothing more.

Two modes:

    pharma_sim serve                 # browse the stored dataset
    pharma_sim serve --live          # host a running plant and stream it
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from pharma_sim.api.live import LiveHub, LiveSimulator
from pharma_sim.api.service import DataService, TelemetryUnavailable
from pharma_sim.config.linter import lint_config
from pharma_sim.config.loader import load_config
from pharma_sim.storage.factory import build_storage

__all__ = ["create_app", "AppSettings"]

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


@dataclass
class AppSettings:
    """How the server is wired. Populated by the CLI."""

    config_dir: str = "config"
    live: bool = False
    seed: int | None = None
    speed: float = 120.0
    warmup_hours: float = 6.0
    sinks: tuple[str, ...] = ()


@dataclass
class AppState:
    settings: AppSettings
    service: DataService | None = None
    storage: Any = None
    hub: LiveHub = field(default_factory=LiveHub)
    live: LiveSimulator | None = None
    config: Any = None


def create_app(settings: AppSettings | None = None) -> FastAPI:
    settings = settings or AppSettings()
    state = AppState(settings=settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state.hub.bind(asyncio.get_running_loop())
        config = load_config(settings.config_dir)
        issues = lint_config(config)
        if issues:
            # Refuse to serve an inconsistent configuration: every number on the
            # dashboard would be suspect.
            raise RuntimeError(
                f"configuration in {settings.config_dir} is inconsistent "
                f"({len(issues)} issue(s)); run `pharma_sim validate`"
            )
        state.config = config

        if settings.live:
            state.live = LiveSimulator(
                settings.config_dir,
                state.hub,
                seed=settings.seed,
                speed=settings.speed,
                warmup_hours=settings.warmup_hours,
                sinks=settings.sinks,
            )
            logger.info(
                "starting the live plant (warm-up %.1f h, then %.0fx)",
                settings.warmup_hours,
                settings.speed,
            )
            await asyncio.to_thread(state.live.start)
            simulator = state.live.simulator
            state.storage = simulator.storage
            state.service = DataService(simulator.storage, config, live_plant=simulator.plant)
        else:
            storage = build_storage(config.storage, reset=False)
            storage.initialise()
            state.storage = storage
            state.service = DataService(storage, config)

        try:
            yield
        finally:
            if state.live is not None:
                await asyncio.to_thread(state.live.stop)
            elif state.storage is not None:
                state.storage.close()

    app = FastAPI(
        title="Pharmaceutical Factory Simulator",
        description=(
            "Read-only API over the simulated plant. No authentication and no "
            "write endpoints by design."
        ),
        version="0.5.0",
        lifespan=lifespan,
    )

    def service() -> DataService:
        if state.service is None:  # pragma: no cover - lifespan guarantees this
            raise HTTPException(503, "data service is not ready")
        return state.service

    # ---------------------------------------------------------------- meta
    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "live": state.settings.live,
            "running": state.live.running if state.live else False,
            "error": state.live.error if state.live else None,
        }

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        if state.live is not None:
            return state.live.status()
        return {
            "live": False,
            "running": False,
            "storage": service().describe_storage(),
            "tables": service().table_counts(),
        }

    @app.get("/api/integrity")
    def integrity() -> dict[str, Any]:
        return service().integrity()

    # --------------------------------------------------------------- topology
    @app.get("/api/plant")
    def plant() -> dict[str, Any]:
        return {"plant": service().plant(), "summary": service().plant_summary()}

    @app.get("/api/units")
    def units() -> list[dict[str, Any]]:
        return service().units()

    @app.get("/api/machines")
    def machines(unit_id: str | None = None) -> list[dict[str, Any]]:
        return service().machines(unit_id)

    @app.get("/api/machines/{machine_id}")
    def machine(machine_id: str) -> dict[str, Any]:
        found = service().machine(machine_id)
        if found is None:
            raise HTTPException(404, f"unknown machine {machine_id}")
        return found

    @app.get("/api/machines/{machine_id}/sensors")
    def machine_sensors(machine_id: str) -> list[dict[str, Any]]:
        return service().machine_sensors(machine_id)

    @app.get("/api/machines/{machine_id}/sensors/{tag}/series")
    def sensor_series(
        machine_id: str,
        tag: str,
        hours: float = Query(6.0, gt=0, le=720),
        limit: int = Query(1200, gt=0, le=20000),
    ) -> dict[str, Any]:
        try:
            return service().sensor_series(machine_id, tag, hours=hours, limit=limit)
        except TelemetryUnavailable as exc:
            raise HTTPException(501, str(exc)) from exc

    @app.get("/api/machines/{machine_id}/events")
    def machine_events(machine_id: str, limit: int = Query(120, le=2000)):
        return service().machine_events(machine_id, limit)

    @app.get("/api/machines/{machine_id}/timeline")
    def machine_timeline(machine_id: str, limit: int = Query(200, le=2000)):
        return service().machine_timeline(machine_id, limit)

    # ---------------------------------------------------------------- batches
    @app.get("/api/batches")
    def batches(limit: int = Query(300, le=5000), disposition: str | None = None):
        return service().batches(limit, disposition)

    @app.get("/api/batches/{batch_id}")
    def batch(batch_id: str) -> dict[str, Any]:
        found = service().batch(batch_id)
        if found is None:
            raise HTTPException(404, f"unknown batch {batch_id}")
        return found

    @app.get("/api/batches/{batch_id}/timeline")
    def batch_timeline(batch_id: str):
        return service().batch_timeline(batch_id)

    # ---------------------------------------------------------------- quality
    @app.get("/api/qc")
    def qc(limit: int = Query(500, le=5000), result: str | None = None):
        return service().qc_results(limit, result)

    @app.get("/api/qc/by-parameter")
    def qc_by_parameter():
        return service().qc_by_parameter()

    @app.get("/api/deviations")
    def deviations(limit: int = Query(300, le=5000)):
        return service().deviations(limit)

    @app.get("/api/rca")
    def rca(limit: int = Query(300, le=5000)):
        return service().rca_reports(limit)

    @app.get("/api/rca/{rca_id}")
    def rca_report(rca_id: str) -> dict[str, Any]:
        found = service().rca_report(rca_id)
        if found is None:
            raise HTTPException(404, f"unknown RCA {rca_id}")
        return found

    @app.get("/api/capa")
    def capa(limit: int = Query(300, le=5000)):
        return service().capas(limit)

    # ------------------------------------------------------------ reliability
    @app.get("/api/failures")
    def failures(limit: int = Query(300, le=5000)):
        return service().failures(limit)

    @app.get("/api/failures/by-category")
    def failures_by_category():
        return service().failures_by_category()

    @app.get("/api/maintenance")
    def maintenance(limit: int = Query(300, le=5000)):
        return service().maintenance(limit)

    # ----------------------------------------------------------------- people
    @app.get("/api/employees")
    def employees(unit_id: str | None = None):
        return service().employees(unit_id)

    @app.get("/api/shifts")
    def shifts(limit: int = Query(120, le=2000)):
        return service().shifts(limit)

    @app.get("/api/employee-events")
    def employee_events(limit: int = Query(300, le=5000)):
        return service().employee_events(limit)

    # ------------------------------------------------------------------ other
    @app.get("/api/events")
    def events(
        limit: int = Query(200, le=5000),
        category: str | None = None,
        severity: str | None = None,
    ):
        return service().events(limit, category, severity)

    @app.get("/api/trends/production")
    def production_trend(limit: int = Query(90, le=1000)):
        return service().production_trend(limit)

    @app.get("/api/trends/oee")
    def oee_trend(scope: str = "PLANT", limit: int = Query(90, le=1000)):
        return service().oee_trend(scope, limit)

    # -------------------------------------------------------------- live feed
    @app.get("/api/live/replay")
    def live_replay(limit: int = Query(1500, le=8000)):
        """Recent messages, so a chart is populated the moment it opens."""
        return state.hub.replay(limit)

    @app.websocket("/ws/live")
    async def live_feed(websocket: WebSocket) -> None:
        await websocket.accept()
        subscriber = state.hub.subscribe()
        try:
            # Send the replay buffer first so charts start populated.
            replay = state.hub.replay(1500)
            if replay:
                await websocket.send_text(
                    json.dumps({"type": "replay", "messages": replay}, default=str)
                )
            while True:
                batch = [await subscriber.queue.get()]
                # Coalesce whatever else is already waiting into one frame: at a
                # thousand messages a second, one frame per message would spend
                # more time in the socket than in the chart.
                while len(batch) < 400:
                    try:
                        batch.append(subscriber.queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                await websocket.send_text(
                    json.dumps(
                        {"type": "batch", "messages": batch, "dropped": subscriber.dropped},
                        default=str,
                    )
                )
        except WebSocketDisconnect:
            pass
        except Exception:  # pragma: no cover - client-side failures
            logger.debug("live websocket closed", exc_info=True)
        finally:
            state.hub.unsubscribe(subscriber)

    # ------------------------------------------------------------- dashboard
    if STATIC_DIR.is_dir():
        app.mount(
            "/static", StaticFiles(directory=str(STATIC_DIR)), name="static"
        )

        @app.get("/")
        def dashboard() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover
        logger.exception("unhandled error on %s", request.url.path)
        return JSONResponse(
            status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"}
        )

    app.state.app_state = state
    return app


def run(settings: AppSettings, host: str = "127.0.0.1", port: int = 8000) -> None:
    """Serve the app with uvicorn."""
    import uvicorn

    uvicorn.run(create_app(settings), host=host, port=port, log_level="info")
