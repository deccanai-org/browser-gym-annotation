"""Serving artifact bytes.

There was no route for this at all: screenshots were uploaded, hashed, dropped,
and referenced from every trajectory step as `attempt/<id>/<id>.jpg` — a relative
path that resolved against the FRONTEND origin and 404'd, so the review UI has
been rendering broken images for as long as it has had images.

Two ways in, deliberately:

* by artifact id — what the UI holds, stable across a re-upload of the same frame;
* by content path — what an EXPORTED bundle carries, so a client that received
  `blobs/ab/cd/<sha>.jpg` can fetch exactly that and verify it against the sha256
  printed next to it.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import blobstore, models
from app.auth import current_annotator
from app.db import get_db

router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])

_MEDIA = {
    "screenshot": "image/jpeg",
    "dom": "text/html; charset=utf-8",
    "axtree": "application/json",
    "som": "application/json",
    "observation": "application/json",
    "video": "video/webm",
}

# A screenshot is immutable once written — it is addressed by its own digest, so
# the bytes behind a uri can never change. Let the browser keep it rather than
# re-fetching every frame each time the annotator scrubs the trajectory.
_CACHE = "public, max-age=31536000, immutable"


def _serve(art: models.Artifact) -> Response:
    data = blobstore.get(art.uri)
    if data is None:
        # A row whose blob was never written (every artifact predating the store)
        # or was lost. 410 rather than 404: the reference was valid, the content
        # is gone — which is what the UI needs to tell them apart.
        raise HTTPException(status_code=410, detail="artifact bytes are not stored")
    return Response(content=data, media_type=_MEDIA.get(art.kind, "application/octet-stream"),
                    headers={"Cache-Control": _CACHE, "X-Artifact-Sha256": art.sha256 or ""})


@router.get("/{artifact_id}")
def get_artifact(artifact_id: UUID,
                 _current: models.Annotator = Depends(current_annotator),
                 db: Session = Depends(get_db)) -> Response:
    art = db.get(models.Artifact, artifact_id)
    if art is None:
        raise HTTPException(status_code=404, detail="unknown artifact")
    return _serve(art)


@router.get("/by-path/{path:path}")
def get_artifact_by_path(path: str,
                         _current: models.Annotator = Depends(current_annotator),
                         db: Session = Depends(get_db)) -> Response:
    """Fetch by the content path an exported bundle carries.

    Resolved through the Artifact ROW rather than straight off disk, so this can
    only ever serve content the platform actually registered — `blobstore.get`
    additionally refuses to read outside the store.
    """
    art = db.scalar(select(models.Artifact).where(models.Artifact.uri == path).limit(1))
    if art is None:
        raise HTTPException(status_code=404, detail="unknown artifact")
    return _serve(art)
