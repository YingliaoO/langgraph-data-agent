"""Top-level package for the knowledge ingestion service."""

__all__ = ["create_app"]


def __getattr__(name):
    # Pure ingestion/fixture helpers must not construct the HTTP app on import.
    if name == "create_app":
        from .main import create_app
        return create_app
    raise AttributeError(name)
