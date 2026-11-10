"""Filter only Codex app-server's model/list response; forward its other RPCs unchanged."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading

from .. import gateway, services


def model_list_response(line, models):
    try:
        request = json.loads(line)
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(request, dict) or request.get("method") != "model/list" or "id" not in request:
        return None
    request_id = request["id"]
    if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
        return None
    response = {"id": request_id}
    if "jsonrpc" in request:
        response["jsonrpc"] = request["jsonrpc"]
    params = request.get("params")
    if params is None:
        params = {}
    cursor = params.get("cursor") if isinstance(params, dict) else None
    limit = params.get("limit") if isinstance(params, dict) else None
    hidden = params.get("includeHidden") if isinstance(params, dict) else None
    valid = (isinstance(params, dict) and (limit is None or (type(limit) is int and limit >= 0))
             and (hidden is None or isinstance(hidden, bool)))
    offset = 0
    if cursor is not None:
        valid = valid and isinstance(cursor, str) and cursor.startswith("ccgw:")
        if valid:
            try:
                offset = int(cursor.removeprefix("ccgw:"))
            except ValueError:
                valid = False
            valid = valid and 0 <= offset <= len(models)
    if not valid:
        response["error"] = {"code": -32602, "message": "Invalid model/list parameters"}
    else:
        size = 100 if limit is None else limit
        end = min(offset + size, len(models))
        next_cursor = f"ccgw:{end}" if size and end < len(models) else None
        response["result"] = {"data": models[offset:end], "nextCursor": next_cursor}
    return (json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def validate_arguments(argv):
    value_options = {"-c", "--config", "--enable", "--disable", "--remote", "--remote-auth-token-env",
                     "-i", "--image", "-m", "--model", "--local-provider", "-p", "--profile",
                     "-s", "--sandbox", "-C", "--cd", "--add-dir", "-a", "--ask-for-approval"}
    index = 0
    while index < len(argv) and argv[index] != "app-server":
        arg = argv[index]
        if arg == "--" or not arg.startswith("-"):
            break
        index += 2 if arg in value_options else 1
    if index >= len(argv) or argv[index] != "app-server":
        raise services.ServiceError("--sync-model-picker requires Codex app-server over stdio (for example Orca)")
    for index, arg in enumerate(argv[index + 1:], index + 1):
        if arg in {"daemon", "proxy", "generate-ts", "generate-json-schema", "--print-startup-config"}:
            raise services.ServiceError("--sync-model-picker requires Codex app-server over stdio")
        if arg == "--listen" and (index + 1 == len(argv) or argv[index + 1] != "stdio://"):
            raise services.ServiceError("--sync-model-picker requires Codex app-server over stdio")
        if arg.startswith("--listen=") and arg != "--listen=stdio://":
            raise services.ServiceError("--sync-model-picker requires Codex app-server over stdio")


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("pipe closed while writing")
        view = view[written:]


def run(command, env, models):
    try:
        child = subprocess.Popen(command, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
    except OSError as error:
        gateway.eprint(f"ERROR: cannot run {command[0]}: {error}")
        return 127

    lock = threading.Lock()
    errors = []
    output_fd = sys.stdout.fileno()

    def forward_line(line):
        response = model_list_response(line, models)
        if response is None:
            _write_all(child.stdin.fileno(), line)
        else:
            with lock:
                _write_all(output_fd, response)

    def forward_input():
        pending = bytearray()
        try:
            while chunk := os.read(sys.stdin.fileno(), 65536):
                search_from = len(pending)
                pending.extend(chunk)
                while (end := pending.find(b"\n", search_from)) >= 0:
                    line = bytes(pending[:end + 1])
                    del pending[:end + 1]
                    search_from = 0
                    forward_line(line)
            if pending:
                forward_line(bytes(pending))
        except BrokenPipeError:
            pass
        except OSError as error:
            errors.append(f"cannot forward Codex app-server input: {error}")
        finally:
            try:
                child.stdin.close()
            except BrokenPipeError:
                pass

    def forward_signal(signum, _frame):
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass

    previous = signal.signal(signal.SIGTERM, forward_signal)
    threading.Thread(target=forward_input, daemon=True).start()
    try:
        try:
            for line in child.stdout:
                with lock:
                    _write_all(output_fd, line)
            result = child.wait()
        except KeyboardInterrupt:
            forward_signal(signal.SIGINT, None)
            result = child.wait()
        except OSError as error:
            errors.append(f"cannot forward Codex app-server output: {error}")
            child.terminate()
            result = child.wait()
    finally:
        signal.signal(signal.SIGTERM, previous)
        child.stdout.close()
    if errors:
        gateway.eprint("ERROR: " + errors[0])
        return 1
    return result if result >= 0 else 128 - result
