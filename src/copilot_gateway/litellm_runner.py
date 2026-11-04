"""Run the LiteLLM proxy in-process with the ccgw source patches installed.

Usage: python -m copilot_gateway.litellm_runner --config PATH --port N
"""
from __future__ import annotations

import argparse
import sys

from . import litellm_patches


def main(argv=None):
    parser = argparse.ArgumentParser(prog="copilot_gateway.litellm_runner")
    parser.add_argument("--config", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)
    try:
        litellm_patches.install()
        litellm_patches.verify()
    except litellm_patches.PatchError as error:
        print(f"ERROR: LiteLLM patch verification failed: {error}", file=sys.stderr)
        return 1
    from litellm.proxy.proxy_cli import run_server

    # One worker keeps the server in this interpreter, where the patch hook lives.
    return run_server.main(args=["--config", args.config, "--host", args.host, "--port", str(args.port),
                                 "--num_workers", "1"], prog_name="litellm", standalone_mode=True)


if __name__ == "__main__":
    sys.exit(main())
