"""
WSGI entrypoint — used by gunicorn and auto-detected by Nixpacks / Railway.

    gunicorn wsgi:app

For local development use run.py directly:

    python run.py
"""
from run import app

__all__ = ["app"]
