"""Core, framework-agnostic feature-flag evaluation logic.

This package has no dependency on Flask or the database layer so that it can
be shared unmodified between the server (app/routes.py) and the client SDK
(feature_flag_client.py), guaranteeing that local (cached) evaluation and
server-side evaluation always agree.
"""

from .evaluation import evaluate_flag, deterministic_bucket

__all__ = ["evaluate_flag", "deterministic_bucket"]
