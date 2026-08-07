"""Where an artifact's BYTES actually live.

`checkpoints.add_artifact` used to hash a screenshot, record its length, and drop
it: `Artifact` carried a sha256 and a byte count for content that existed nowhere.
Every shipped golden sample referenced `attempt/<id>/<id>.jpg` for a file the
platform had never written, so the one thing a buyer checks first — open a
screenshot, see the page the annotator saw — failed on every row.

Content-addressed, because the alternative is worse. Keying on the digest means
the same frame captured twice costs one file, a re-upload is idempotent, and the
bytes can be verified against the row that references them. It also makes the
store safe to sync to object storage later: the key IS the content, so a copy can
never be stale.

Layout: <root>/<aa>/<bb>/<sha256><ext>, fanned out two levels so no directory
holds more than a few thousand entries.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from app.config import settings

# Extensions we are willing to write. An artifact kind maps to exactly one, so a
# `uri` can never claim a type its bytes are not.
_EXT = {
    "screenshot": ".jpg",
    "dom": ".html",
    "axtree": ".json",
    "som": ".json",
    "observation": ".json",
    "video": ".webm",
}


def root() -> Path:
    return Path(settings.artifact_root).expanduser()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def uri_for(sha: str, kind: str) -> str:
    """The stable relative path for this content. Relative on purpose: it is what
    ships inside an exported bundle, and an absolute host path or a signed URL
    would be meaningless (or expired) by the time a client reads it."""
    ext = _EXT.get(kind, ".bin")
    return f"blobs/{sha[:2]}/{sha[2:4]}/{sha}{ext}"


def path_for(sha: str, kind: str) -> Path:
    return root() / uri_for(sha, kind)


def put(data: bytes, kind: str) -> tuple[str, str]:
    """Write bytes, return `(sha256, uri)`. Idempotent — identical content is
    written once.

    The write goes to a temp file in the destination directory and is then
    atomically renamed, so a crash or a concurrent writer can never leave a
    half-written blob that still hashes correctly by name. Two processes racing
    on the same digest both succeed and the loser's rename simply replaces
    identical bytes.
    """
    sha = digest(data)
    dest = path_for(sha, kind)
    if dest.exists():
        return sha, uri_for(sha, kind)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return sha, uri_for(sha, kind)


def api_url(artifact_id) -> str:
    """Where the UI fetches this artifact.

    `TrajectoryStep.screenshot_url` used to hold the raw storage path, which the
    browser resolved against the FRONTEND origin and got a 404 — the review UI
    rendered a broken image for every step that had one. It holds this instead;
    the portable content path still ships in the export, next to its sha256.
    """
    return f"/api/artifacts/{artifact_id}"


def get(uri: str) -> bytes | None:
    """The bytes behind a stored uri, or None when the row outlived its blob.

    None rather than an exception: 39 artifact rows predate the store and can
    never be filled in, and a missing screenshot must degrade to "no image" in
    the UI rather than failing the request that was fetching a trajectory.
    """
    if not uri:
        return None
    p = (root() / uri).resolve()
    try:
        # Refuse to read outside the store even if a row's uri was tampered with.
        p.relative_to(root().resolve())
    except ValueError:
        return None
    try:
        return p.read_bytes()
    except (OSError, ValueError):
        return None


def exists(uri: str) -> bool:
    return get(uri) is not None
