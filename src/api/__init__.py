"""
API package: exposes the FastAPI application that mounts the shared /api/v1 router.
This used to be a Flask stub, which shadowed src/api.py and made the FastAPI tests
run against the wrong app. Flask is not used anywhere in this project.
"""

from fastapi import FastAPI

from src.api.routes import engine, router
from src.config import settings

app = FastAPI(
    title="Incident Triage Engine API",
    description=(
        "Automated log parser, context compressor, runbook retrieval and LLM "
        "root-cause triage engine."
    ),
    version=settings.VERSION,
)

app.include_router(router)

__all__ = ["app", "engine", "router"]
