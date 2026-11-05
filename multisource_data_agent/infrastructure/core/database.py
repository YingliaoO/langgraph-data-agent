"""
Database configuration helpers and session utilities.
"""

from contextlib import contextmanager
from typing import Dict, Generator, Iterator

from sqlalchemy import LargeBinary, and_, cast, create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from multisource_data_agent.config import settings

_DEFAULT_LOG_ECHO = bool(settings.DEBUG)

_connect_args: Dict[str, bool] = {}
if settings.APP_DATABASE_URL.startswith("sqlite"):
    _connect_args = {"check_same_thread": False}

engine = create_engine(
    settings.APP_DATABASE_URL,
    echo=_DEFAULT_LOG_ECHO,
    future=True,
    pool_pre_ping=True,
    connect_args=_connect_args,
)

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
    class_=Session,
)

Base = declarative_base()


def owner_filter(column, owner_id: str):
    """JWT subjects are opaque/case-sensitive, including on MySQL's usual CI collations.

    Keep the indexed normal equality and add byte equality so case/trailing-space
    variants cannot inherit another subject's sessions or uploads.
    """
    return and_(column == owner_id, cast(column, LargeBinary) == owner_id.encode("utf-8"))


def init_db(*, create_all: bool = False) -> None:
    """
    Initialize the database engine.

    If `create_all` is True the metadata for all models is created.
    """
    if create_all:
        # Lazy import to avoid circular dependencies during startup.
        import multisource_data_agent.infrastructure.persistence.db.models  # noqa: F401

        Base.metadata.create_all(bind=engine)


def get_db() -> Generator[Session, None, None]:
    """
    Provide a database session dependency for FastAPI routes.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope(*, commit: bool = True) -> Iterator[Session]:
    """
    Context manager that handles commit/rollback automatically.
    """
    session = SessionLocal()
    try:
        yield session
        if commit:
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
