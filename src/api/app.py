"""
FastAPI Application Factory for ObservaSage Dashboard.

Run locally with:
    .venv/bin/uvicorn src.api.app:app --reload --port 8000
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from src.api.routes import router as api_router
from src.api.websocket import ws_router

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(
    cases_root: Path | str | None = None,
    results_dir: Path | str | None = None,
) -> FastAPI:
    """Creates and configures the ObservaSage FastAPI application."""
    app = FastAPI(
        title="ObservaSage RCA Dashboard",
        description="Multi-Signal Root Cause Analysis (Logs + Metrics + Traces) extending LogSage.",
        version="1.0.0",
    )

    app.state.cases_root = Path(cases_root) if cases_root is not None else PROJECT_ROOT / "data" / "ground_truth" / "rcaeval"
    app.state.results_dir = Path(results_dir) if results_dir is not None else PROJECT_ROOT / "results"

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:8000", "http://localhost:8000"],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    app.include_router(api_router)
    app.include_router(ws_router)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/health", include_in_schema=False)
    def root_health() -> dict[str, str]:
        return {"status": "ok", "version": "1.0.0"}

    return app


app = create_app()
