"""Run the LiteLLM proxy in-process with the ccgw source patches installed.

Usage: python -m copilot_gateway.litellm_runner --config PATH --port N [--state PATH] [--refresh-minutes M]

With ``--state`` the proxy applies model-list changes in its config file
without a restart (see ``hot_reload``); ``--refresh-minutes`` also re-fetches
the Copilot catalog on that interval.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from . import litellm_patches


def refresh_catalog():
    from . import gateway
    from .settings import Settings
    changed = gateway.refresh(Settings.load())
    if any(changed.values()):
        print("ccgw hot-reload: catalog changed: " + ", ".join(name for name, value in changed.items() if value),
              file=sys.stderr, flush=True)


def install_hot_reload(config, state, refresh_minutes):
    from litellm.proxy import proxy_server

    from . import hot_reload

    def publish(router):
        proxy_server.llm_model_list = router.get_model_list()

    watcher = hot_reload.Watcher(
        Path(config), Path(state), Path(config).read_text(encoding="utf-8"), lambda: proxy_server.llm_router,
        refresh=refresh_catalog, refresh_seconds=refresh_minutes * 60, after_apply=publish)
    hot_reload.install(proxy_server.app, watcher)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="copilot_gateway.litellm_runner")
    parser.add_argument("--config", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--state", help="hot-reload status file (enables model-list hot reload)")
    parser.add_argument("--refresh-minutes", type=float, default=0.0)
    args = parser.parse_args(argv)
    try:
        litellm_patches.install()
        litellm_patches.verify()
    except litellm_patches.PatchError as error:
        print(f"ERROR: LiteLLM patch verification failed: {error}", file=sys.stderr)
        return 1
    if args.state:
        install_hot_reload(args.config, args.state, args.refresh_minutes)
    from litellm.proxy.proxy_cli import run_server

    # One worker keeps the server in this interpreter, where the patch hook lives.
    return run_server.main(args=["--config", args.config, "--host", args.host, "--port", str(args.port),
                                 "--num_workers", "1"], prog_name="litellm", standalone_mode=True)


if __name__ == "__main__":
    sys.exit(main())
