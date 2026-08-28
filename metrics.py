"""Per-call usage metrics for the ua-mcp tool surface.

One JSON line per tool call, appended to a size-capped rotating file. The data
answers three questions: is ua-mcp actually used (adoption), where is it weak
(empty results, errors, dead tools), and is the same question being asked over
and over (caller + arg_hash within a time window).

Hard rule: writing a metric must never break a tool call. Every failure path is
swallowed, and the writer disables itself for good after repeated errors.

Env vars:
  UA_MCP_METRICS_FILE — output path. Empty string disables metrics entirely.
"""

from __future__ import annotations

import contextvars
import functools
import hashlib
import inspect
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from typing import Any

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1
DEFAULT_PATH = "/var/log/ua-mcp/metrics.jsonl"
MAX_ARG_CHARS = 500
MAX_BYTES = 50 * 1024 * 1024  # 50MB per file
BACKUP_COUNT = 10  # → 500MB hard cap; /data has 1.9T free
MAX_CONSECUTIVE_FAILURES = 5

# Sentinels used to classify a tool's return value. These mirror literal
# strings in server.py; test_outcome_sentinels_still_exist_in_server_source
# fails loudly if any of them is reworded there.
_INBAND_ERROR_PREFIX = "Error: "
_NOTFOUND_MARK = " not found"
_EMPTY_PREFIX = "No "
_EMPTY_MARKS = (
    " has no relationships",
    " has no extends/implements",
    "does not call any other functions",
)

# ---------------------------------------------------------------------------
# Request-scoped context
#
# server.py sets these in its HTTP middleware; the tool wrapper reads them.
# Verified: a contextvar set in a Starlette BaseHTTPMiddleware does reach the
# tool body, even though the call crosses call_next, the session manager's
# lifespan task group (stateless branch) and the MCP message dispatch loop.
# ---------------------------------------------------------------------------

caller_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ua_mcp_caller", default=None
)
session_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ua_mcp_session", default=None
)

# ---------------------------------------------------------------------------
# Writer state
# ---------------------------------------------------------------------------

_handler: RotatingFileHandler | None = None
_disabled = False
_failures = 0


def _disable(reason: str) -> None:
    """Turn metrics off for the rest of the process, complaining exactly once."""
    global _disabled, _handler
    _disabled = True
    if _handler is not None:
        try:
            _handler.close()
        except Exception:
            pass
        _handler = None
    print(f"[ua-mcp-metrics] WARN disabled: {reason}", file=sys.stderr)


def _reset() -> None:
    """Test hook: drop writer state so the next record() re-reads the env."""
    global _handler, _disabled, _failures
    if _handler is not None:
        try:
            _handler.close()
        except Exception:
            pass
    _handler = None
    _disabled = False
    _failures = 0


def _get_handler() -> RotatingFileHandler | None:
    global _handler
    if _disabled:
        return None
    if _handler is not None:
        return _handler

    path = os.environ.get("UA_MCP_METRICS_FILE", DEFAULT_PATH)
    if not path:
        _disable("UA_MCP_METRICS_FILE is empty")
        return None

    try:
        handler = RotatingFileHandler(
            path,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError as exc:
        _disable(f"cannot open {path}: {type(exc).__name__}")
        return None

    handler.setFormatter(logging.Formatter("%(message)s"))
    _handler = handler
    return _handler


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------


def caller_key(ip: str | None, user_agent: str | None) -> str:
    """Group calls by origin without storing the raw IP.

    stateless_http=True means there is no mcp-session-id to group by, so this
    stands in for one: same origin + a time window approximates a session.
    """
    raw = f"{ip or ''}\x00{user_agent or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def arg_hash(tool: str, args: dict[str, Any]) -> str:
    """Stable fingerprint of a call, used to count repeated questions.

    Computed on the *untruncated* args: truncating first would make two long,
    distinct queries collide.
    """
    payload = json.dumps(args, sort_keys=True, separators=(",", ":"), default=repr)
    raw = f"{tool}\x00{payload}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def _truncate(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) > MAX_ARG_CHARS:
            return value[:MAX_ARG_CHARS] + "…"
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _truncate(repr(value))


def classify(
    result: str | None, exc: BaseException | None
) -> tuple[str, str | None]:
    """Map a tool outcome to (outcome, err).

    Reads the returned text rather than parsing JSON: no tool in server.py
    returns JSON, they all return human-readable prose. Ordered — first match
    wins.

    'notfound' is kept separate from 'empty' on purpose: notfound means the AI
    passed a bad id (a prompt problem), empty means the graph had nothing to
    give (a ua-data problem).
    """
    if exc is not None:
        return "error", type(exc).__name__
    if not isinstance(result, str):
        return "ok", None
    if result.startswith(_INBAND_ERROR_PREFIX):
        # server.py swallows exceptions in ~18 places and returns "Error: ...".
        # Without this branch the error rate would read 0% forever.
        return "error", "in-band"
    if _NOTFOUND_MARK in result:
        return "notfound", None
    if result.startswith(_EMPTY_PREFIX) or any(m in result for m in _EMPTY_MARKS):
        return "empty", None
    return "ok", None


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def record(
    *,
    tool: str,
    project: str | None,
    args: dict[str, Any],
    ms: int,
    result: Any,
    exc: BaseException | None,
) -> None:
    """Append one metric line. Never raises."""
    global _failures
    try:
        handler = _get_handler()
        if handler is None:
            return

        outcome, err = classify(result if isinstance(result, str) else None, exc)
        line = json.dumps(
            {
                "ts": datetime.now(UTC)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                "caller": caller_var.get(),
                "sid": session_var.get(),
                "tool": tool,
                "project": project,
                "args": {k: _truncate(v) for k, v in args.items()},
                "arg_hash": arg_hash(tool, args),
                "ms": ms,
                "outcome": outcome,
                "res_bytes": (
                    len(result.encode("utf-8")) if isinstance(result, str) else None
                ),
                "err": err,
                "v": SCHEMA_VERSION,
            },
            ensure_ascii=False,
            default=repr,
        )

        log_record = logging.makeLogRecord(
            {"msg": line, "levelno": logging.INFO, "levelname": "INFO"}
        )

        failed = False
        # logging swallows emit errors into handleError, so intercept both
        # routes to keep the consecutive-failure counter honest.
        original_handle_error = handler.handleError

        def _on_error(_rec: logging.LogRecord) -> None:
            nonlocal failed
            failed = True

        handler.handleError = _on_error  # type: ignore[method-assign]
        try:
            handler.emit(log_record)
        except Exception:
            failed = True
        finally:
            handler.handleError = original_handle_error  # type: ignore[method-assign]

        if failed:
            _failures += 1
            if _failures >= MAX_CONSECUTIVE_FAILURES:
                _disable(f"{_failures} consecutive write failures")
        else:
            _failures = 0
    except Exception:
        # A metrics bug must never surface to the caller.
        pass


# ---------------------------------------------------------------------------
# Installation into FastMCP
# ---------------------------------------------------------------------------


def _wrap(fn):
    """Measure a tool call. Returns and raises exactly what fn does."""
    # Resolved once at decoration time — it never changes, and re-deriving it
    # on every call would put reflection on the hot path.
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        signature = None

    @functools.wraps(fn)
    def inner(*args: Any, **kwargs: Any):
        start = time.perf_counter()
        result: Any = None
        exc: BaseException | None = None
        try:
            result = fn(*args, **kwargs)
            return result
        except BaseException as raised:
            exc = raised
            raise
        finally:
            # record() swallows its own errors, but this outer guard also
            # covers a broken/monkeypatched record and the arg binding itself.
            # An exception escaping here would replace the tool's return value.
            try:
                ms = int((time.perf_counter() - start) * 1000)
                try:
                    if signature is None:
                        raise TypeError("no signature")
                    call_args = dict(signature.bind(*args, **kwargs).arguments)
                except Exception:
                    call_args = dict(kwargs)
                record(
                    tool=fn.__name__,
                    project=call_args.get("project"),
                    args=call_args,
                    ms=ms,
                    result=result,
                    exc=exc,
                )
            except Exception:
                pass

    return inner


def install(mcp: Any) -> None:
    """Wrap every tool registered after this call.

    Must run before the first @mcp.tool() decorator executes — they run at
    import time in source order. Verified: this leaves the generated MCP tool
    schema byte-identical, because functools.wraps preserves the signature,
    annotations and docstring FastMCP reads.
    """
    original_tool = mcp.tool

    def tool(*args: Any, **kwargs: Any):
        decorate = original_tool(*args, **kwargs)

        def apply(fn):
            return decorate(_wrap(fn))

        return apply

    mcp.tool = tool
