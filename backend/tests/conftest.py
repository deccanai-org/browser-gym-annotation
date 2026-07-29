"""Test fixtures. The API runs against an in-memory SQLite DB (via a get_db
override) so tests need no Postgres and stay isolated per test.

Auth is enforced on the API, so the default `client` is authenticated as a seeded
test annotator. Use `client_for(email)` to get a client acting as a DIFFERENT
annotator (multi-annotator / ownership tests), and `anon_client` for the
unauthenticated (401) path."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import auth as authmod
from app.config import settings
from app.db import Base, get_db
from app.main import app
from app.models import Annotator

TEST_EMAIL = "test@deccan.ai"


@pytest.fixture()
def _engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)


@pytest.fixture()
def _session_factory(_engine):
    return sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)


def _ensure_annotator(factory, email: str, role: str = "annotator") -> None:
    with factory() as db:
        if db.scalar(select(Annotator).where(Annotator.email == email)) is None:
            db.add(Annotator(email=email, password_hash=authmod.hash_password("testpass1"), role=role))
            db.commit()


@pytest.fixture(autouse=True)
def _no_real_db_at_boot(monkeypatch, _session_factory):
    """The lifespan's restart reconciler resolves its OWN session from
    app.db.SessionLocal, which the get_db override does not touch — so every
    TestClient(app) boot ran three DESTRUCTIVE reconcilers against whatever
    database happened to be configured, and one of them SIGTERMs pids read from
    lease rows. Point that seam at the test database too; `pytest` must never be
    able to reach production state, let alone signal a process."""
    import app.db as db_mod
    import app.main as main_mod

    monkeypatch.setattr(db_mod, "SessionLocal", _session_factory)
    monkeypatch.setattr(main_mod, "SessionLocal", _session_factory, raising=False)


@pytest.fixture()
def client(_engine, _session_factory):
    def override_get_db():
        db = _session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        _ensure_annotator(_session_factory, TEST_EMAIL)
        c.cookies.set(settings.auth_cookie, authmod.make_token(TEST_EMAIL))
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def client_for(_engine, _session_factory):
    """Factory → a TestClient authenticated as an arbitrary (auto-created) annotator.
    Lets a test act as several distinct accounts (ownership / QA aggregation)."""

    def override_get_db():
        db = _session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    made = []

    def _make(email: str, role: str = "annotator") -> TestClient:
        _ensure_annotator(_session_factory, email, role)
        c = TestClient(app)
        c.cookies.set(settings.auth_cookie, authmod.make_token(email))
        made.append(c)
        return c

    yield _make
    for c in made:
        c.close()
    app.dependency_overrides.clear()


@pytest.fixture()
def reviewer_client(_engine, _session_factory):
    """A TestClient authenticated as a REVIEWER — QA adjudication and the
    dataset-wide export are privileged surfaces, not open to every annotator."""

    def override_get_db():
        db = _session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    email = "reviewer@deccan.ai"
    with TestClient(app) as c:
        _ensure_annotator(_session_factory, email, "reviewer")
        c.cookies.set(settings.auth_cookie, authmod.make_token(email))
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def anon_client(_engine, _session_factory):
    """An UNauthenticated client — for asserting protected routes 401."""
    def override_get_db():
        db = _session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def db_session(_engine, _session_factory):
    """A read session bound to the same DB the API writes to (for assertions)."""
    db = _session_factory()
    try:
        yield db
    finally:
        db.close()
