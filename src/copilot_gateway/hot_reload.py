"""Apply regenerated model lists to a running LiteLLM proxy without restarting it.

The runner wraps the proxy's lifespan with a watcher task on the server's
event loop. When the generated config changes, only ``model_list`` and the
hidden aliases (``router_settings.model_group_alias``) are swapped in the live
router; any other difference needs a restart and is reported as such. The
watcher records what it applied in ``<data>/run/<service>.loaded`` so
``ccgw``/``ccp``/``ccx`` can wait for it instead of restarting the service.
Optionally it also re-fetches the Copilot catalog on an interval.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import os
import sys
import time

from . import catalog
from .settings import write_private

ROUTED_SETTING = "model_group_alias"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def log(message):
    print(f"ccgw hot-reload: {message}", file=sys.stderr, flush=True)


def static_part(config):
    """Everything a hot reload cannot change."""
    rest = {key: value for key, value in config.items() if key not in ("model_list", "router_settings")}
    router = {key: value for key, value in (config.get("router_settings") or {}).items() if key != ROUTED_SETTING}
    return rest, router


def read_state(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def write_state(path, applied, needs_restart=None):
    write_private(path, json.dumps({"pid": os.getpid(), "applied": applied, "needs_restart": needs_restart}) + "\n")


def apply(router, config):
    model_list = config.get("model_list")
    if not isinstance(model_list, list) or not model_list:
        raise ValueError("config has no model_list")
    aliases = (config.get("router_settings") or {}).get(ROUTED_SETTING) or {}
    # Synchronous on the event loop, so no request observes a half-built router.
    router.set_model_list(copy.deepcopy(model_list))
    router.update_settings(**{ROUTED_SETTING: copy.deepcopy(aliases)})


class Watcher:
    def __init__(self, config_path, state_path, loaded_text, get_router, refresh=None, refresh_seconds=0.0,
                 poll_seconds=2.0, clock=time.monotonic, after_apply=lambda router: None):
        self.config_path, self.state_path = config_path, state_path
        self.baseline = static_part(catalog.parse_rendered(loaded_text))
        self.current, self.failed = digest(loaded_text), None
        self.get_router, self.refresh, self.after_apply = get_router, refresh, after_apply
        self.refresh_seconds, self.poll_seconds, self.clock = refresh_seconds, poll_seconds, clock
        self.next_refresh = clock() + refresh_seconds if refresh and refresh_seconds > 0 else None

    def check(self, first=False):
        """Apply the config file if it changed; returns True when the live router was updated."""
        try:
            text = self.config_path.read_text(encoding="utf-8")
        except OSError as error:
            log(f"cannot read {self.config_path}: {error}")
            return False
        sha = digest(text)
        if sha == self.current:
            if first:
                write_state(self.state_path, sha)
            return False
        if sha == self.failed:
            return False
        try:
            config = catalog.parse_rendered(text)
            if static_part(config) != self.baseline:
                log(f"{self.config_path.name} changed beyond the model list; restart needed (ccgw restart)")
                write_state(self.state_path, self.current, needs_restart=sha)
                self.current = sha
                return False
            router = self.get_router()
            if router is None:
                raise RuntimeError("LiteLLM router is not initialised")
            apply(router, config)
            self.after_apply(router)
        except Exception as error:  # noqa: BLE001 - a bad file must not take the proxy down
            log(f"kept the previous model list; {self.config_path.name} not applied: {error}")
            self.failed = sha
            return False
        self.current = sha
        write_state(self.state_path, sha)
        log(f"applied {len(config['model_list'])} models from {self.config_path.name}")
        return True

    async def maybe_refresh(self):
        if self.next_refresh is None or self.clock() < self.next_refresh:
            return
        self.next_refresh = self.clock() + self.refresh_seconds
        try:
            await asyncio.to_thread(self.refresh)
        except Exception as error:  # noqa: BLE001 - keep serving the current list
            log(f"catalog refresh failed: {error}")

    async def run(self):
        self.check(first=True)
        while True:
            await asyncio.sleep(self.poll_seconds)
            await self.maybe_refresh()
            self.check()


def install(app, watcher):
    """Run `watcher` for the lifetime of the proxy (after LiteLLM's own startup)."""
    original = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(app_):
        async with original(app_) as state:
            task = asyncio.create_task(watcher.run())
            try:
                yield state
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app.router.lifespan_context = lifespan
