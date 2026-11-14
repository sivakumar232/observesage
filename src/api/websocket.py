"""
WebSocket streaming endpoint for live ObservaSage RCA diagnosis progress.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from src.api.routes import artifact_to_api_payload
from src.pipeline.orchestrator import ALL_MODES, ObservaSagePipeline

ws_router = APIRouter(tags=["WebSocket"])


@ws_router.websocket("/ws/diagnose")
async def websocket_diagnose(websocket: WebSocket) -> None:
    await websocket.accept()
    cases_root: Path = websocket.app.state.cases_root
    results_dir: Path = websocket.app.state.results_dir

    try:
        raw = await websocket.receive_text()
        payload = json.loads(raw)
        case_id = str(payload.get("case_id", "")).strip()
        mode = str(payload.get("mode", "fusion")).strip()
        use_llm = bool(payload.get("use_llm", False))
        force_recompute = bool(payload.get("force_recompute", False))

        if not case_id or "/" in case_id or "\\" in case_id or ".." in case_id:
            await websocket.send_json({"stage": "error", "detail": "Invalid case_id"})
            await websocket.close(code=1008)
            return

        if mode not in ALL_MODES:
            await websocket.send_json({"stage": "error", "detail": f"Unsupported mode '{mode}'"})
            await websocket.close(code=1008)
            return

        case_dir = (cases_root / case_id).resolve()
        if not case_dir.is_relative_to(cases_root.resolve()) or not case_dir.is_dir():
            await websocket.send_json({"stage": "error", "detail": f"Case '{case_id}' not found"})
            await websocket.close(code=1008)
            return

        # --- Check persisted CaseAnalysisArtifact cache first ---
        if not force_recompute and not use_llm:
            cached_art = ObservaSagePipeline.load_artifact(results_dir, case_id, mode)
            if cached_art is not None:
                await websocket.send_json({"stage": "complete", **artifact_to_api_payload(cached_art)})
                return

        # --- Live staged pipeline run (single convert_case + retrieve, persists artifact) ---
        await websocket.send_json({
            "stage": "ingesting",
            "progress": 25,
            "message": f"Parsing parquet telemetry for {case_id}...",
        })
        pipeline = ObservaSagePipeline(cases_root=cases_root, use_llm=use_llm)
        snapshot, gt_info = pipeline.load_case(case_dir)

        await websocket.send_json(
            {
                "stage": "retrieving",
                "progress": 60,
                "message": f"Running Telemetry-RAG ({mode}) across {len(snapshot.logs)} log services, {len(snapshot.metrics)} metrics, {len(snapshot.traces)} traces...",
            }
        )
        rag_ctx = pipeline.retrieve_context(snapshot, mode=mode)  # type: ignore[arg-type]

        top_cand = rag_ctx.candidate_services[0] if rag_ctx.candidate_services else "unknown"
        await websocket.send_json(
            {
                "stage": "fusing",
                "progress": 85,
                "message": f"Fusing signals (top candidate: {top_cand}, {rag_ctx.token_count} tokens)...",
            }
        )
        _, _, artifact = pipeline.run_case_full(
            case_dir=case_dir,
            mode=mode,  # type: ignore[arg-type]
            snapshot=snapshot,
            gt_info=gt_info,
            rag_ctx=rag_ctx,
        )
        pipeline.save_artifact(artifact, results_dir)

        await websocket.send_json({
            "stage": "complete",
            "progress": 100,
            "message": "Diagnosis complete!",
            **artifact_to_api_payload(artifact),
        })
    except WebSocketDisconnect:
        return
    except (json.JSONDecodeError, ValueError, RuntimeError, OSError) as exc:
        await websocket.send_json({"stage": "error", "detail": str(exc)})
        await websocket.close(code=1011)
