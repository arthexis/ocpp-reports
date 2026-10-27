"""Backward-compatible import path; canonical implementation: warehouse.ingestion."""
from .warehouse.ingestion import *  # noqa: F401,F403
from .warehouse.ingestion import _schema as _schema
from .warehouse.ingestion import _canonical as _canonical
