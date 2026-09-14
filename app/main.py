"""FastAPI application: edit ingest, licensing analysis, snapshot read-back."""

from __future__ import annotations

import os
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from .engine import GraphError, analyze, rat_json, validate_submission
from .models import (
    AnalysisReport,
    AnalysisRequest,
    AnalysisSnapshot,
    AnalysisSummary,
    EditCreated,
    EditSubmission,
    EditSummary,
    StoredEdit,
)
from .store import Store


def create_app(db_path: str | None = None) -> FastAPI:
    store = Store(db_path or os.environ.get("APP_DB", "app.db"))
    app = FastAPI(
        title="Podcast Edit Consent API",
        version="1.0.0",
        summary="Interval-provenance licensing checks for the school podcast club.",
    )

    @app.exception_handler(GraphError)
    async def graph_error_handler(_request, exc: GraphError):
        # Locatable 422, same shape as FastAPI's validation errors.
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {
                        "loc": ["body", *exc.loc],
                        "msg": exc.msg,
                        "type": "graph_error",
                    }
                ]
            },
        )

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/edits", status_code=201, response_model=EditCreated)
    def create_edit(sub: EditSubmission):
        # The whole document is validated before anything is stored; on any
        # graph error the submission is rejected as a whole (nothing saved).
        durations = validate_submission(sub)
        edit_id = uuid.uuid4().hex
        store.create_edit(edit_id, sub.model_dump(mode="json"))
        return {
            "edit_id": edit_id,
            "durations": {nid: rat_json(d) for nid, d in durations.items()},
        }

    @app.get("/edits", response_model=list[EditSummary])
    def list_edits():
        return store.list_edits()

    @app.get("/edits/{edit_id}", response_model=StoredEdit)
    def get_edit(edit_id: str):
        found = store.get_edit(edit_id)
        if found is None:
            raise HTTPException(status_code=404, detail="edit not found")
        return found

    @app.post("/edits/{edit_id}/analyses", status_code=201, response_model=AnalysisReport)
    def create_analysis(edit_id: str, req: AnalysisRequest):
        found = store.get_edit(edit_id)
        if found is None:
            raise HTTPException(status_code=404, detail="edit not found")
        sub = EditSubmission.model_validate(found["edit"])
        report = analyze(sub, req.output, req.audience)
        analysis_id = uuid.uuid4().hex
        full_report = {"analysis_id": analysis_id, "edit_id": edit_id, **report}
        store.create_analysis(analysis_id, edit_id, req.model_dump(), full_report)
        return full_report

    @app.get("/edits/{edit_id}/analyses", response_model=list[AnalysisSummary])
    def list_analyses(edit_id: str):
        if store.get_edit(edit_id) is None:
            raise HTTPException(status_code=404, detail="edit not found")
        return store.list_analyses(edit_id)

    @app.get("/edits/{edit_id}/analyses/{analysis_id}", response_model=AnalysisSnapshot)
    def get_analysis(edit_id: str, analysis_id: str):
        found = store.get_analysis(edit_id, analysis_id)
        if found is None:
            raise HTTPException(status_code=404, detail="analysis not found")
        return found

    return app


app = create_app()
