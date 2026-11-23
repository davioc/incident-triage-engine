"""Engine and session plumbing for report storage.

Persistence is opt-in: an empty DATABASE_URL means storage is disabled rather than
pointing at a half-configured database, so the stack still runs offline.
"""

from contextlib import contextmanager
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.config import settings

_engine: Optional[Engine] = None
_engine_url: Optional[str] = None


def storage_enabled() -> bool:
    """True only when a DATABASE_URL is configured."""
    return bool(settings.DATABASE_URL.strip())


def get_engine(url: Optional[str] = None) -> Engine:
    """Return a cached engine, rebuilding it when the URL changes.

    ``url`` overrides the configured value; tests use it to point the same
    repository code at SQLite instead of Postgres.
    """
    global _engine, _engine_url
    target = (url or settings.DATABASE_URL).strip()
    if not target:
        raise RuntimeError(
            "DATABASE_URL is not set; report storage is disabled. Set DATABASE_URL "
            "or run the stack with the Postgres service."
        )

    if _engine is not None and _engine_url == target:
        return _engine

    parsed = make_url(target)
    kwargs = {}
    if parsed.drivername.startswith("sqlite") and parsed.database in ("", ":memory:"):
        # An in-memory SQLite database only survives if every session shares one
        # connection; a second connection starts with an empty database.
        kwargs = {"connect_args": {"check_same_thread": False}, "poolclass": StaticPool}

    _engine = create_engine(target, future=True, **kwargs)
    _engine_url = target
    return _engine


def reset_engine() -> None:
    """Drop the cached engine. Tests flip DATABASE_URL between cases."""
    global _engine, _engine_url
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _engine_url = None


@contextmanager
def session_scope(url: Optional[str] = None):
    """Yield a session that commits on success and rolls back on any error."""
    session = Session(get_engine(url))
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def public_url(url: Optional[str] = None) -> str:
    """The configured URL without credentials, safe to show in responses and logs."""
    return make_url((url or settings.DATABASE_URL).strip()).render_as_string(
        hide_password=True
    )
