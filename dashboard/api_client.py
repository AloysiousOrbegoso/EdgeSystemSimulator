"""
Dashboard API client — thin wrapper over httpx for talking to the scheduler.

Every page imports this. The dashboard does no business logic of its own:
it reads from the scheduler service, displays it, and triggers actions
through these methods.

Errors are returned as raised exceptions so the calling page can surface
them (Streamlit's `st.error` is the usual handler). Timeouts are short on
read-only calls and longer on action calls (scenario runs can take 30+ s
for drain even on the smallest scenarios).
"""

from __future__ import annotations

import os
from typing import Any

import httpx


DEFAULT_BASE_URL = os.environ.get("SCHEDULER_BASE_URL", "http://localhost:8000")


class SchedulerClient:
    """Wraps the scheduler's HTTP API for the dashboard."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        read_timeout: float = 5.0,
        action_timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url
        self._read_timeout = read_timeout
        self._action_timeout = action_timeout

    # ---------------- low-level helpers ----------------

    def _client(self, timeout: float) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, timeout=timeout)

    def _get(self, path: str) -> Any:
        with self._client(self._read_timeout) as c:
            r = c.get(path)
            r.raise_for_status()
            return r.json()

    def _post(self, path: str, json: Any = None, *, action: bool = False) -> Any:
        timeout = self._action_timeout if action else self._read_timeout
        with self._client(timeout) as c:
            r = c.post(path, json=json)
            r.raise_for_status()
            try:
                return r.json()
            except Exception:
                return None

    # ---------------- system ----------------

    def health(self) -> dict:
        return self._get("/health")

    def system_state(self) -> dict:
        return self._get("/system/state")

    def learned_state(self) -> dict:
        return self._get("/system/learned_state")

    # ---------------- nodes ----------------

    def nodes(self) -> list[dict]:
        return self._get("/nodes").get("nodes", [])

    def register_node(self, spec: dict) -> dict:
        return self._post("/nodes/register", json=spec)

    # ---------------- algorithms ----------------

    def algorithms(self) -> dict:
        return self._get("/config/algorithms")

    def set_algorithm(self, name: str) -> dict:
        return self._post("/config/algorithm", json={"name": name})

    # ---------------- queue + tasks ----------------

    def queue(self) -> dict:
        return self._get("/queue")

    def submit_task(self, task: dict) -> dict:
        return self._post("/submit_task", json=task)

    def submit_batch(self, tasks: list[dict]) -> dict:
        return self._post("/submit_batch", json=tasks)

    # ---------------- trials ----------------

    def trial_active(self) -> dict:
        return self._get("/trial/active")

    def trial_start(self, payload: dict) -> dict:
        return self._post("/trial/start", json=payload)

    def trial_end(self) -> dict:
        return self._post("/trial/end", action=True)

    def trial_reset(self) -> dict:
        return self._post("/trial/reset", action=True)