"""Liveness endpoint. Deliberately checks nothing external: it answers
whether this process is up, which is what nginx probes in Phase 6."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
