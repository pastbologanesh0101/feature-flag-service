"""Feature Flag Service client SDK.

Two ways to use it:

1. Remote mode (simple, one HTTP call per evaluation)::

    client = FeatureFlagClient("http://localhost:5000", local_cache=False)
    result = client.evaluate("new-checkout", user_id="u123", attributes={"country": "US"})

2. Local-cache mode (recommended for production/high QPS code paths)::

    client = FeatureFlagClient("http://localhost:5000")   # local_cache=True by default
    # ... client fetches ALL flag configs once at startup, then evaluates
    # every subsequent check locally, with zero network round trips, using
    # exactly the same evaluation engine as the server (ffs_core.evaluation).
    result = client.evaluate("new-checkout", user_id="u123", attributes={"country": "US"})

    # A background thread refreshes the cache every ``poll_interval`` seconds
    # so flag changes made in the dashboard/API eventually propagate without
    # the caller ever blocking on a network call.
    client.stop()  # call when shutting down to stop the background poller
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Iterable, List, Optional

import requests

from ffs_core.evaluation import evaluate_flag


class FeatureFlagClient:
    def __init__(
        self,
        base_url: str,
        local_cache: bool = True,
        poll_interval: Optional[float] = 30.0,
        auto_start: bool = True,
        session: Optional[Any] = None,
        timeout: float = 10.0,
    ):
        """
        Args:
            base_url: root URL of the Feature Flag Service, e.g.
                "http://localhost:5000".
            local_cache: when True (default), fetch all flag configs and
                evaluate locally, with no network call per check. When
                False, every ``evaluate``/``evaluate_bulk`` call hits the
                server's ``/evaluate`` or ``/evaluate/bulk`` endpoint.
            poll_interval: seconds between background cache refreshes in
                local-cache mode. Pass ``None`` to disable background
                polling entirely (only the initial sync happens) -- useful
                for short-lived scripts and for tests that must observe an
                exact fetch count.
            auto_start: when True (default), the initial sync (local-cache
                mode) happens immediately in the constructor.
            session: inject a ``requests``-compatible session (or a mock)
                for testing.
            timeout: per-request timeout in seconds.
        """
        self.base_url = base_url.rstrip("/")
        self.local_cache = local_cache
        self.poll_interval = poll_interval
        self.timeout = timeout
        self._session = session if session is not None else requests.Session()

        self._flags_cache: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.fetch_count = 0

        if self.local_cache and auto_start:
            self.start()

    # -- lifecycle ----------------------------------------------------

    def start(self) -> None:
        """Perform the initial sync and (optionally) start background polling."""
        self.sync()
        if self.poll_interval:
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._poll_loop, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Stop the background polling thread, if running."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _poll_loop(self) -> None:
        while not self._stop_event.wait(self.poll_interval):
            try:
                self.sync()
            except Exception:
                # Keep serving the last-known-good cache if a refresh fails.
                pass

    # -- syncing --------------------------------------------------------

    def sync(self) -> None:
        """Fetch every flag config from the server in one call and cache it."""
        resp = self._session.get(f"{self.base_url}/flags", timeout=self.timeout)
        resp.raise_for_status()
        flags = resp.json()
        self.fetch_count += 1
        with self._lock:
            self._flags_cache = {f["key"]: f for f in flags}

    # -- evaluation -------------------------------------------------------

    def evaluate(
        self,
        flag_key: str,
        user_id: Optional[str] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not self.local_cache:
            resp = self._session.post(
                f"{self.base_url}/evaluate",
                json={
                    "flag_key": flag_key,
                    "user_id": user_id,
                    "attributes": attributes or {},
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return resp.json()

        with self._lock:
            flag = self._flags_cache.get(flag_key)
        if flag is None:
            return {"flag_key": flag_key, "value": None, "reason": "flag_not_found"}
        return evaluate_flag(flag, user_id=user_id, attributes=attributes)

    def evaluate_bulk(
        self,
        flag_keys: Optional[Iterable[str]] = None,
        user_id: Optional[str] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        if not self.local_cache:
            resp = self._session.post(
                f"{self.base_url}/evaluate/bulk",
                json={
                    "flag_keys": list(flag_keys) if flag_keys is not None else None,
                    "user_id": user_id,
                    "attributes": attributes or {},
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return resp.json()

        with self._lock:
            keys: List[str] = (
                list(flag_keys) if flag_keys is not None else list(self._flags_cache.keys())
            )
        return {k: self.evaluate(k, user_id=user_id, attributes=attributes) for k in keys}
