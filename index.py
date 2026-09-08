"""Vercel entry point for PROMETHEUS.

Vercel detects the exported FastAPI instance from this supported root-level
entrypoint and routes the dashboard and API paths to the same application.
"""

from prometheus.api.app import app

__all__ = ["app"]
