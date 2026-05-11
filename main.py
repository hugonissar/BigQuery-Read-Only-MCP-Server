import copy
import io
import ipaddress
import os
import re
import asyncio
import json
import logging
import sys
import uuid
import hmac
import hashlib
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, date
from decimal import Decimal
from typing import Any
import sqlparse
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from google.cloud import bigquery
from google.api_core import exceptions as gcp_exceptions

if sys.version_info < (3, 11):
    raise RuntimeError(
        "Python 3.11+ is required for asyncio.timeout(). "
        "Update your base image (e.g. python:3.12-slim)."
    )


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bq_mcp")

_VALID_LOG_LEVELS = frozenset({"debug", "info", "warning", "error", "critical"})


def _log(level: str, **kwargs):
    """
    Emit a structured JSON log entry at the specified severity level.

    Accepts a log level string (one of "debug", "info", "warning", "error",
    "critical") and any number of keyword arguments that will be serialised
    as JSON fields in the log message. Raises ValueError immediately if
    an invalid level is supplied, preventing silent misfires. The kwargs
    are also attached to the LogRecord under the ``bq_fields`` key for
    downstream log processors that parse structured fields.

    :param level: Severity level string; must be in _VALID_LOG_LEVELS.
    :param kwargs: Arbitrary key/value pairs included in the JSON payload.
    :raises ValueError: If ``level`` is not a recognised log level.
    """
    if level not in _VALID_LOG_LEVELS:
        raise ValueError(
            f"Invalid log level '{level}'. Must be one of: {sorted(_VALID_LOG_LEVELS)}"
        )
    getattr(log, level)(json.dumps(kwargs), extra={"bq_fields": kwargs})


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def _require_env(key: str) -> str:
    """
    Read a required environment variable and return its stripped string value.

    Looks up ``key`` in the process environment and strips leading/trailing
    whitespace from the result. Raises RuntimeError if the variable is absent
    or empty, making misconfigured deployments fail fast at startup rather than
    producing cryptic downstream errors. This is intentionally strict — every
    variable guarded by this function is needed for the server to function.

    :param key: Name of the environment variable to retrieve.
    :returns: The non-empty, whitespace-stripped value of the variable.
    :raises RuntimeError: If the variable is unset or resolves to an empty string.
    """
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Required environment variable '{key}' is not set.")
    return val.strip()


def _parse_allowed_pairs(datasets_str: str, tables_str: str) -> list[tuple[str, str]]:
    """
    Parse comma-separated dataset and table lists into (dataset, table) pairs.

    Splits both inputs on commas, strips whitespace from each entry, and
    positionally zips them: the table at index i is paired with the dataset at
    index i. Both inputs must contain the same number of non-empty entries. A
    single-value input (no commas) is fully supported — it simply produces a
    one-element list, preserving the original single-table deployment shape.
    Duplicate (dataset, table) pairs are rejected because they would create
    redundant, confusing configuration. The same table name MAY appear in
    multiple datasets — the validator will require fully-qualified references
    to such names so they remain unambiguous at query time.

    :param datasets_str: Comma-separated dataset names from BQ_DATASET_ID.
    :param tables_str:   Comma-separated table names from BQ_ALLOWED_TABLE.
    :returns: List of (dataset, table) tuples in declaration order.
    :raises RuntimeError: If lengths mismatch, any entry is empty, or duplicate
                          (dataset, table) pairs are present.
    """
    datasets = [s.strip() for s in datasets_str.split(",")]
    tables   = [s.strip() for s in tables_str.split(",")]

    if any(not d for d in datasets):
        raise RuntimeError("BQ_DATASET_ID contains an empty entry.")
    if any(not t for t in tables):
        raise RuntimeError("BQ_ALLOWED_TABLE contains an empty entry.")
    if len(datasets) != len(tables):
        raise RuntimeError(
            f"BQ_DATASET_ID has {len(datasets)} entries but BQ_ALLOWED_TABLE has "
            f"{len(tables)}. They must contain the same number of comma-separated "
            "values so each table is positionally paired with its dataset."
        )

    pairs = list(zip(datasets, tables))
    if len(set(pairs)) != len(pairs):
        raise RuntimeError(
            "BQ_DATASET_ID / BQ_ALLOWED_TABLE contains duplicate (dataset, table) pairs."
        )
    return pairs


def _load_config() -> dict:
    """
    Read all configuration values from environment variables and return them
    as a single flat dictionary.

    Required variables (MCP_API_KEY, GCP_PROJECT_ID, BQ_DATASET_ID,
    BQ_ALLOWED_TABLE) are fetched via _require_env and will abort startup if
    missing. BQ_DATASET_ID and BQ_ALLOWED_TABLE BOTH accept comma-separated
    values to expose multiple tables — entry i of BQ_DATASET_ID is paired with
    entry i of BQ_ALLOWED_TABLE, so the two lists must contain the same number
    of values. The parsed result is stored under "ALLOWED_PAIRS" as a list of
    (dataset, table) tuples and is the single source of truth for table policy
    throughout the codebase. Optional variables fall back to documented
    defaults. Byte-based limits (MAX_BYTES) are derived from their MB
    counterparts so the rest of the codebase always works in bytes. If
    MCP_ADMIN_KEY is unset, a warning is emitted and the API key is reused for
    admin endpoints.

    :returns: A dict containing every runtime configuration parameter, keyed
              by uppercase snake-case names (e.g. "MAX_MB", "RATE_LIMIT_QPM",
              "ALLOWED_PAIRS").
    """
    max_mb  = int(os.getenv("MAX_SCAN_MB", "100"))
    api_key = _require_env("MCP_API_KEY")

    admin_key_raw = os.getenv("MCP_ADMIN_KEY", "").strip()
    if not admin_key_raw:
        admin_key_raw = api_key
        log.warning(
            "MCP_ADMIN_KEY is not set. Admin endpoints share the MCP client key. "
            "Set a separate MCP_ADMIN_KEY in production."
        )

    allowed_pairs = _parse_allowed_pairs(
        _require_env("BQ_DATASET_ID"),
        _require_env("BQ_ALLOWED_TABLE"),
    )

    return {
        "API_KEY":              api_key,
        "ADMIN_KEY":            admin_key_raw,
        "PROJECT_ID":           _require_env("GCP_PROJECT_ID"),
        "ALLOWED_PAIRS":        allowed_pairs,
        "MAX_MB":               max_mb,
        "MAX_ROWS":             int(os.getenv("MAX_RESULT_ROWS",          "2000")),
        "JOB_TIMEOUT":          int(os.getenv("BQ_JOB_TIMEOUT_SECS",      "30")),
        "MAX_SQL_LEN":          int(os.getenv("MAX_SQL_LENGTH",            "2000")),
        "MAX_BYTES":            max_mb * 1024 * 1024,
        "SCHEMA_TTL":           int(os.getenv("SCHEMA_TTL_SECS",           "300")),
        "DRY_RUN_CACHE_TTL":    int(os.getenv("DRY_RUN_CACHE_TTL_SECS",    "60")),
        "DRY_RUN_CACHE_MAX":    int(os.getenv("DRY_RUN_CACHE_MAX_ENTRIES", "1000")),
        "RATE_LIMIT_QPM":       int(os.getenv("RATE_LIMIT_QPM",            "20")),
        "RATE_LIMIT_BURST":     int(os.getenv("RATE_LIMIT_BURST",          "5")),
        "ADMIN_RATE_LIMIT_QPM": int(os.getenv("ADMIN_RATE_LIMIT_QPM",     "10")),
        "QUERY_CONCURRENCY":    int(os.getenv("QUERY_CONCURRENCY",         "10")),
        "META_CONCURRENCY":     int(os.getenv("META_CONCURRENCY",          "3")),
        "MAX_BODY_BYTES":       int(os.getenv("MAX_REQUEST_BODY_BYTES",    str(64 * 1024))),
    }


_cfg: dict | None = None
_cfg_lock = threading.Lock()


def get_cfg() -> dict:
    """
    Return the singleton configuration dictionary, loading it on first call.

    Uses double-checked locking with a module-level threading.Lock to ensure
    the config is loaded exactly once even under concurrent startup. After the
    first successful load, subsequent calls return the cached dict immediately
    with no locking overhead. The resulting dict is shared across all threads
    and must be treated as read-only.

    :returns: The fully-populated configuration dict produced by _load_config().
    """
    global _cfg
    if _cfg is None:
        with _cfg_lock:
            if _cfg is None:
                _cfg = _load_config()
    return _cfg


# ---------------------------------------------------------------------------
# Thread pools — initialised in lifespan from config
# ---------------------------------------------------------------------------
_meta_executor:  ThreadPoolExecutor | None = None
_query_executor: ThreadPoolExecutor | None = None

_query_sem: asyncio.Semaphore | None = None
_meta_sem:  asyncio.Semaphore | None = None


async def _try_acquire(sem: asyncio.Semaphore) -> bool:
    """
    Attempt a non-blocking acquire on an asyncio.Semaphore.

    Wraps the acquire coroutine in asyncio.timeout(0) so the call returns in
    the current event-loop tick: if a token is available it is consumed and
    True is returned; if the semaphore is exhausted (value == 0) a TimeoutError
    fires before any suspension occurs and False is returned instead. This
    pattern is race-free on Python 3.11+ — the deprecated locked()+acquire()
    two-step had a window where another coroutine could drain the last token
    between the two awaits.

    :param sem: The asyncio.Semaphore to attempt to acquire.
    :returns: True if the semaphore was acquired (caller must release it);
              False if no token was available.
    """
    try:
        async with asyncio.timeout(0):
            await sem.acquire()
        return True
    except TimeoutError:
        return False


# ---------------------------------------------------------------------------
# Per-client rate limiter
# ---------------------------------------------------------------------------
class _RateLimiter:
    """
    Token-bucket rate limiter keyed by client IP.

    Instantiate once per rate class (normal vs. admin) and pass the
    config key names so limits are read from environment variables.
    """

    def __init__(
        self,
        qpm_cfg_key:   str = "RATE_LIMIT_QPM",
        burst_cfg_key: str = "RATE_LIMIT_BURST",
    ) -> None:
        self._buckets: dict[str, dict] = {}
        self._lock         = threading.Lock()
        self._qpm_cfg_key   = qpm_cfg_key
        self._burst_cfg_key = burst_cfg_key

    @staticmethod
    def _normalise_client_id(ip: str) -> str:
        """
        Collapse an IPv6 address to its /64 network prefix for rate-limiting.

        Clients that rotate ephemeral IPv6 suffixes (/128 addresses) per
        connection would otherwise each receive a fresh token bucket, effectively
        circumventing rate limits. Grouping all addresses sharing a /64 prefix
        into one bucket closes that loophole. IPv4 addresses and any IPv6
        address that fails network parsing are returned unchanged.

        :param ip: Raw IP address string (IPv4 or IPv6).
        :returns: The /64 network address string for IPv6 (e.g. "2001:db8::/64"),
                  or the original ``ip`` string for IPv4 or on parse failure.
        """
        if ":" in ip:
            try:
                network = ipaddress.ip_network(f"{ip}/64", strict=False)
                return str(network.network_address) + "/64"
            except ValueError:
                pass
        return ip

    @staticmethod
    def _real_client_ip(
        headers:      dict[bytes, bytes],
        scope_client: str,
    ) -> str:
        """
        Resolve the genuine originating IP from ASGI scope and request headers.

        On Cloud Run, the TCP peer reported in the ASGI scope "client" field is
        always the Google load balancer, not the end user. Cloud Run prepends the
        actual client IP as the first comma-separated value in X-Forwarded-For
        before forwarding the request, making that entry trustworthy. If the
        header is absent (direct/non-proxied deployment), scope_client is already
        the correct address and is returned as-is.

        :param headers: Lowercase ASGI header dict (bytes keys and values).
        :param scope_client: The TCP peer IP string from the ASGI scope.
        :returns: The real originating IP string to use for rate-limiting.
        """
        xff = headers.get(b"x-forwarded-for", b"").decode("utf-8", errors="replace")
        if xff:
            first = xff.split(",")[0].strip()
            if first:
                return first
        return scope_client

    def is_allowed(self, client_id: str) -> bool:
        """
        Decide whether a request from ``client_id`` is within the rate limit.

        Implements a token-bucket algorithm: each client starts with ``burst``
        tokens and refills at ``qpm / 60`` tokens per second up to the burst
        cap. One token is consumed per allowed request. New client IDs are
        seeded with ``burst - 1`` tokens so the very first request is always
        permitted. The QPM and burst values are read from config on every call
        so live config changes take effect without a restart.

        :param client_id: Normalised client identifier (typically an IP address).
        :returns: True if the request is within quota; False if it should be
                  rejected with a 429-style response.
        """
        client_id = self._normalise_client_id(client_id)
        cfg   = get_cfg()
        rate  = cfg[self._qpm_cfg_key] / 60.0
        burst = float(cfg[self._burst_cfg_key])

        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(client_id)
            if bucket is None:
                self._buckets[client_id] = {"tokens": burst - 1.0, "last": now}
                return True

            elapsed          = now - bucket["last"]
            bucket["tokens"] = min(burst, bucket["tokens"] + elapsed * rate)
            bucket["last"]   = now

            if bucket["tokens"] >= 1.0:
                bucket["tokens"] -= 1.0
                return True
            return False

    def cleanup(self, max_age: float = 3600.0) -> None:
        """
        Evict token-bucket entries for clients that have been idle too long.

        Uses a two-phase approach to minimise lock-hold time: Phase 1 scans
        the bucket dict under a brief lock to build a list of stale keys (pure
        memory traversal). Phase 2 re-acquires the lock to delete each key,
        re-checking the timestamp to avoid evicting a bucket that became active
        between the two phases. This keeps the lock held for two short O(n)
        passes rather than one combined pass that includes deletion side-effects.

        :param max_age: Seconds of inactivity after which a client bucket is
                        considered stale and eligible for removal. Defaults to
                        3600 (one hour).
        """
        now = time.monotonic()

        # Phase 1 — identify stale keys.
        with self._lock:
            stale = [k for k, v in self._buckets.items() if now - v["last"] > max_age]

        if not stale:
            return

        # Phase 2 — delete with re-verification.
        with self._lock:
            for k in stale:
                entry = self._buckets.get(k)
                if entry and now - entry["last"] > max_age:
                    del self._buckets[k]


_rate_limiter       = _RateLimiter(qpm_cfg_key="RATE_LIMIT_QPM",       burst_cfg_key="RATE_LIMIT_BURST")
_admin_rate_limiter = _RateLimiter(qpm_cfg_key="ADMIN_RATE_LIMIT_QPM", burst_cfg_key="RATE_LIMIT_BURST")

_current_client: ContextVar[str] = ContextVar("_current_client", default="unknown")


# ---------------------------------------------------------------------------
# Dry-run result cache
# ---------------------------------------------------------------------------
class _DryRunCache:
    """
    LRU cache for BigQuery dry-run byte estimates.

    Constructed with baked-in TTL and size so the hot query path never
    calls get_cfg() during cache lookups.
    """

    def __init__(self, ttl: int, max_entries: int) -> None:
        self._ttl         = ttl
        self._max_entries = max_entries
        self._cache: OrderedDict[str, tuple[int, float]] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _key(sql: str) -> str:
        """
        Derive a compact, collision-resistant cache key from a SQL string.

        Computes the SHA-256 digest of the UTF-8-encoded SQL and returns it as
        a 64-character lowercase hex string. Using a cryptographic hash rather
        than the raw SQL keeps key sizes predictable and bounded regardless of
        query length, and makes accidental prefix collisions practically
        impossible. The resulting key is used as the OrderedDict lookup token
        in both get() and set().

        :param sql: The SQL string to hash, typically the validated+qualified form.
        :returns: 64-character lowercase hex SHA-256 digest of ``sql``.
        """
        return hashlib.sha256(sql.encode()).hexdigest()

    def get(self, sql: str) -> int | None:
        """
        Retrieve a cached dry-run byte estimate for the given SQL string.

        Hashes ``sql`` to look up the entry in the LRU OrderedDict. If the
        entry exists and has not exceeded the configured TTL, the cached byte
        count is returned and the entry is promoted to the most-recently-used
        position. Expired entries are deleted on access to keep memory bounded.
        Thread-safe via an internal threading.Lock.

        :param sql: The SQL query to look up, in its validated and qualified form.
        :returns: The estimated bytes-scanned integer if a fresh cache hit
                  is found, or None on a miss or expiry.
        """
        key = self._key(sql)
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            bytes_est, ts = entry
            if time.monotonic() - ts > self._ttl:
                del self._cache[key]
                return None
            self._cache.move_to_end(key)
            return bytes_est

    def set(self, sql: str, bytes_est: int) -> None:
        """
        Store a dry-run byte estimate in the LRU cache.

        Inserts or refreshes the entry keyed by the SHA-256 hash of ``sql``,
        recording the current monotonic timestamp alongside the estimate. If
        the entry already exists its position is promoted to most-recently-used.
        After insertion, the oldest entry is evicted in a loop until the cache
        size is at or below the configured maximum. Thread-safe via an internal
        threading.Lock.

        :param sql: The SQL query whose scan estimate is being cached.
        :param bytes_est: BigQuery's estimated bytes-processed for the query.
        """
        key = self._key(sql)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = (bytes_est, time.monotonic())
            while len(self._cache) > self._max_entries:
                self._cache.popitem(last=False)


_dry_run_cache: _DryRunCache | None = None


# ---------------------------------------------------------------------------
# BigQuery client
# ---------------------------------------------------------------------------
class BigQueryManager:
    def __init__(self) -> None:
        self.client: bigquery.Client | None = None
        self._schema_cache: tuple[float, dict] | None = None
        self._schema_lock = threading.Lock()

    def setup(self) -> None:
        """
        Initialise the BigQuery client and bind it to the configured GCP project.

        Creates a google.cloud.bigquery.Client scoped to the project ID from
        config and stores it in self.client for use by all subsequent BigQuery
        operations. Also emits an info-level structured log entry to confirm
        initialisation. Must be called once during application startup (inside
        the lifespan context) before any tool invocations attempt to use
        self.client.
        """
        self.client = bigquery.Client(project=get_cfg()["PROJECT_ID"])
        _log("info", event="bq_client_initialised")

    def close(self) -> None:
        """
        Gracefully close the underlying BigQuery HTTP session.

        Calls close() on the google.cloud.bigquery.Client to release connection
        pool resources and emits a structured log entry. Safe to call even if
        setup() was never invoked — the None guard prevents an AttributeError.
        Should be called during application shutdown inside the lifespan
        finally block to avoid resource leaks.
        """
        if self.client:
            self.client.close()
        _log("info", event="bq_client_closed")

    def fetch_schema(self) -> dict:
        """
        Return schema metadata for every allowed table, using a TTL-based cache.

        Iterates over every (dataset, table) pair in ALLOWED_PAIRS and fetches
        partition_field, clustering_fields, and column information for each from
        BigQuery, returning the combined result as ``{"tables": [...]}`` where
        the list preserves the configuration order. The whole result is cached
        for SCHEMA_TTL seconds. Double-checked locking is used so only one
        thread performs the network RPCs while others wait; last-write-wins on
        concurrent cache fills is safe because both threads fetch the same data.
        Returns a deep copy of the cached dict on every call so callers cannot
        mutate the cache's internal table list.

        :returns: A dict with a single "tables" key whose value is a list of
                  per-table dicts each containing "dataset", "table",
                  "partition_field", "clustering_fields", and "schema"
                  (a list of {"name": str, "type": str} dicts).
        :raises RuntimeError: If the BigQuery client has not been initialised
                              via setup().
        """
        cfg = get_cfg()

        with self._schema_lock:
            if self._schema_cache is not None:
                cached_at, schema = self._schema_cache
                if time.monotonic() - cached_at < cfg["SCHEMA_TTL"]:
                    return copy.deepcopy(schema)
                _log("info", event="schema_cache_expired")

        if self.client is None:
            raise RuntimeError("BigQuery client not initialised")

        tables_info: list[dict] = []
        for ds, tbl in cfg["ALLOWED_PAIRS"]:
            table_ref = f"{cfg['PROJECT_ID']}.{ds}.{tbl}"
            table     = self.client.get_table(table_ref)
            tables_info.append({
                "dataset":           ds,
                "table":             tbl,
                "partition_field":   table.time_partitioning.field if table.time_partitioning else None,
                "clustering_fields": table.clustering_fields,
                "schema": [{"name": f.name, "type": f.field_type} for f in table.schema],
            })

        schema = {"tables": tables_info}

        with self._schema_lock:
            self._schema_cache = (time.monotonic(), schema)
            _log("info", event="schema_cached", table_count=len(tables_info))

        return copy.deepcopy(schema)

    def invalidate_schema_cache(self) -> None:
        """
        Force the schema cache to be treated as expired on the next fetch.

        Sets the internal _schema_cache tuple to None under the schema lock,
        causing the next call to fetch_schema() to perform fresh BigQuery RPCs
        for every allowed table instead of returning stale cached data. Used
        by the admin /admin/invalidate-schema endpoint when one or more tables'
        schemas have changed and operators need immediate consistency without
        waiting for the TTL to expire naturally.
        """
        with self._schema_lock:
            self._schema_cache = None


bq = BigQueryManager()


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
mcp = FastMCP("bigquery-mcp", host="0.0.0.0", stateless_http=True)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Manage startup and shutdown of all shared async and thread resources.

    On startup: reads QUERY_CONCURRENCY and META_CONCURRENCY from config to
    create correctly-sized ThreadPoolExecutors and asyncio Semaphores; constructs
    the _DryRunCache with TTL/size from config; calls bq.setup() to open the
    BigQuery client; and launches a background cleanup task that evicts stale
    rate-limiter buckets every hour. The MCP session manager is also started
    here so streaming HTTP tool calls are available throughout the app lifetime.
    On shutdown (finally block): cancels the cleanup task, closes the BigQuery
    client, and shuts down both executors (with cancel_futures=True to discard
    queued-but-not-started work while allowing already-running threads to finish).

    :param app: The FastAPI application instance (unused directly, required by
                the lifespan protocol).
    :yields: Control to FastAPI for the duration of the application's life.
    """
    global _query_sem, _meta_sem, _dry_run_cache, _meta_executor, _query_executor

    cfg = get_cfg()

    _meta_executor  = ThreadPoolExecutor(
        max_workers=cfg["META_CONCURRENCY"],
        thread_name_prefix="bq-meta",
    )
    _query_executor = ThreadPoolExecutor(
        max_workers=cfg["QUERY_CONCURRENCY"],
        thread_name_prefix="bq-query",
    )

    _query_sem = asyncio.Semaphore(cfg["QUERY_CONCURRENCY"])
    _meta_sem  = asyncio.Semaphore(cfg["META_CONCURRENCY"])

    _dry_run_cache = _DryRunCache(
        ttl=cfg["DRY_RUN_CACHE_TTL"],
        max_entries=cfg["DRY_RUN_CACHE_MAX"],
    )

    bq.setup()

    async def _cleanup_loop() -> None:
        while True:
            await asyncio.sleep(3600)
            _rate_limiter.cleanup()
            _admin_rate_limiter.cleanup()
            _log("info", event="rate_limiter_cleanup_ran")

    cleanup_task = asyncio.create_task(_cleanup_loop())
    async with mcp.session_manager.run():
        try:
            yield
        finally:
            cleanup_task.cancel()
            bq.close()
            _meta_executor.shutdown(wait=True,  cancel_futures=True)
            _query_executor.shutdown(wait=True, cancel_futures=True)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="BigQuery MCP Server", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """
    Attach a correlation ID to every HTTP response for end-to-end traceability.

    Reads the x-request-id header from the incoming request if present;
    otherwise generates a fresh UUID4. After the downstream handler completes,
    the same ID is written into the x-request-id response header so clients and
    log aggregators can correlate requests with responses across all paths —
    including MCP tool-call endpoints. The ID is never interpreted or validated,
    only echoed.

    :param request: The incoming FastAPI/Starlette Request object.
    :param call_next: Callable that passes the request to the next handler and
                      returns the Response.
    :returns: The Response produced by downstream handlers, augmented with the
              x-request-id header.
    """
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    response   = await call_next(request)
    response.headers["x-request-id"] = request_id
    return response


class _AuthMiddleware:
    """
    Pure ASGI middleware: authenticates every request before it reaches the
    application, enforces body-size limits, and rejects credentials in URLs.
    """

    def __init__(self, app) -> None:
        self._app = app

    async def __call__(self, scope, receive, send) -> None:
        """
        ASGI entry point that enforces authentication and security policies.

        Passes non-HTTP/WebSocket scopes (e.g. lifespan) through immediately.
        The /health path is exempted from authentication so load-balancer probes
        work without credentials. For all other paths, the method: (1) resolves
        the real client IP from X-Forwarded-For; (2) rejects requests that
        supply API keys in the query string to prevent credential leakage in
        logs; (3) enforces the MAX_BODY_BYTES limit using Content-Length when
        declared; (4) validates the x-api-key header with a timing-safe
        comparison; and (5) stores the client IP in a ContextVar for downstream
        rate-limiting before delegating to the wrapped ASGI app.

        :param scope: ASGI connection scope dict.
        :param receive: ASGI receive callable.
        :param send: ASGI send callable.
        """
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return

        path = scope.get("path", "")

        if path == "/health":
            await self._app(scope, receive, send)
            return

        raw_headers = scope.get("headers", [])
        headers     = {k.lower(): v for k, v in raw_headers}
        scope_host  = (scope.get("client") or ("unknown",))[0]

        client_host = _RateLimiter._real_client_ip(headers, scope_host)

        qs = scope.get("query_string", b"").decode("utf-8", errors="replace")
        if re.search(r"(?:^|&)(?:api[_-]?key|x-api-key|x-admin-key)=", qs, re.IGNORECASE):
            _log("error", event="api_key_in_query_string", path=path, client=client_host)
            if scope["type"] == "http":
                body = json.dumps({
                    "detail": "API key must be supplied via the x-api-key header, not the query string."
                }).encode()
                await self._send_error(scope, send, 400, body, headers)
            return

        cfg = get_cfg()
        try:
            content_length = int(headers.get(b"content-length", b"0"))
        except (ValueError, TypeError):
            content_length = 0

        if content_length > cfg["MAX_BODY_BYTES"]:
            _log("warning", event="request_body_too_large",
                 path=path, client=client_host, content_length=content_length)
            if scope["type"] == "http":
                body = json.dumps({"detail": "Request body too large."}).encode()
                await self._send_error(scope, send, 413, body, headers)
            return

        api_key = headers.get(b"x-api-key", b"").decode("utf-8", errors="replace").strip()
        try:
            key_ok = hmac.compare_digest(api_key, cfg["API_KEY"])
        except Exception:
            key_ok = False

        if not key_ok:
            _log("warning", event="unauthorised_request", path=path, client=client_host)
            if scope["type"] == "http":
                body = json.dumps({"detail": "Unauthorized"}).encode()
                await self._send_error(scope, send, 403, body, headers)
            return

        token = _current_client.set(client_host)
        try:
            await self._app(scope, receive, send)
        finally:
            _current_client.reset(token)

    @staticmethod
    async def _send_error(scope, send, status: int, body: bytes, headers: dict) -> None:
        """
        Write a minimal JSON HTTP error response directly to the ASGI send callable.

        Constructs and sends the http.response.start and http.response.body ASGI
        events for a given status code and pre-serialised JSON body, bypassing
        FastAPI's response machinery (which is unavailable this early in the
        middleware stack). Also emits an x-request-id response header using the
        value from the incoming request headers, or a newly-generated UUID if none
        was supplied.

        :param scope: ASGI connection scope dict (used only for type checking
                      at the call site; not read inside this method).
        :param send: ASGI send callable to write the response events.
        :param status: HTTP status code to return (e.g. 400, 403, 413).
        :param body: Pre-serialised UTF-8 JSON bytes forming the response body.
        :param headers: Lowercase bytes-keyed request headers dict, used to
                        extract or generate the x-request-id correlation header.
        """
        request_id = (
            headers.get(b"x-request-id", b"").decode("utf-8", errors="replace")
            or str(uuid.uuid4())
        )
        await send({
            "type":    "http.response.start",
            "status":  status,
            "headers": [
                (b"content-type",   b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"x-request-id",   request_id.encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})


app.add_middleware(_AuthMiddleware)


# ---------------------------------------------------------------------------
# SQL validation helpers
# ---------------------------------------------------------------------------

def _format_allowed_pairs(pairs: list[tuple[str, str]]) -> str:
    """
    Render an allowed (dataset, table) pair list as a human-readable string.

    Used in policy-violation error messages so the client sees exactly which
    fully-qualified targets are permitted. The output is a simple
    comma-separated list of ``dataset.table`` identifiers in the same order as
    declared in BQ_DATASET_ID / BQ_ALLOWED_TABLE.

    :param pairs: List of (dataset, table) tuples from cfg["ALLOWED_PAIRS"].
    :returns: A string like "ds1.tbl1, ds2.tbl2".
    """
    return ", ".join(f"{ds}.{tbl}" for ds, tbl in pairs)


def _strip_cte(sql: str) -> str:
    """
    Remove a leading WITH … AS (…) CTE preamble and return the core statement.

    Walks the SQL character-by-character tracking parenthesis depth so it
    correctly handles nested subqueries inside CTE bodies. After the outermost
    closing parenthesis of each CTE is found, it skips whitespace and commas
    and recurses for chained CTEs (WITH a AS (…), b AS (…)). Returns the
    remainder of the string starting at the first non-CTE token (typically
    SELECT). Returns an empty string if the input is malformed, which causes
    the downstream SELECT guard to produce a clean policy-violation error
    instead of a confusing parse exception.

    :param sql: Lowercased, comment-stripped SQL string, potentially starting
                with WITH.
    :returns: The core query string after all CTE definitions have been removed,
              or "" if the CTE structure could not be parsed.
    """
    stripped = sql.strip()
    if not stripped.lower().startswith("with"):
        return stripped

    i = stripped.lower().index("with") + len("with")
    depth      = 0
    found_open = False

    while i < len(stripped):
        ch = stripped[i]
        if ch == "(":
            depth      += 1
            found_open  = True
        elif ch == ")":
            depth -= 1
            if found_open and depth == 0:
                i += 1
                while i < len(stripped) and stripped[i] in (" ", "\t", "\n", ","):
                    i += 1
                if i >= len(stripped):
                    return ""
                remainder = stripped[i:]
                if remainder.lower().strip().startswith("with"):
                    return _strip_cte(remainder)
                if re.match(r"\w+\s+as\s*\(", remainder.lower().lstrip()):
                    return _strip_cte("with " + remainder.lstrip())
                return remainder
        i += 1

    return ""


def _remove_backtick_quotes(sql: str) -> str:
    """
    Strip BigQuery-style backtick identifier quoting from a SQL string.

    Replaces every backtick-quoted token (e.g. `identifier`) with the
    bare identifier text it wraps, using a simple non-greedy regex. This
    normalises quoted references like `project.dataset.table` into
    "project.dataset.table" so that downstream regex checks can match table
    names consistently regardless of whether the author used backtick quoting.
    The function operates on the raw SQL string and does not parse or tokenise.

    :param sql: SQL string potentially containing backtick-quoted identifiers.
    :returns: The same SQL string with all backtick quoting removed.
    """
    return re.sub(r"`([^`]+)`", r"\1", sql)


def _assert_no_extra_tables(normalised_sql: str, outer_sql: str | None = None) -> None:
    """
    Raise ValueError if the SQL references any table outside the allowed pairs.

    Performs three complementary checks to catch table references that bypass
    simpler pattern matching: (1) scans all FROM/JOIN targets and validates each
    against the configured (dataset, table) pairs, treating CTE names as exempt;
    (2) parses the FROM clause of the outermost SELECT for comma-joined tables,
    iteratively collapsing parenthesised subexpressions so nested subqueries
    don't produce false positives; (3) allows UNNEST and LATERAL pseudo-table
    keywords. For each reference: a bare name (no dot) must match an allowed
    table name AND be unambiguous — a bare reference is rejected if the same
    table name appears in multiple allowed datasets; a qualified name
    (dataset.table or project.dataset.table) must match an allowed pair exactly
    on its last two segments. Both ``normalised_sql`` (lowercased, full query)
    and ``outer_sql`` (the core SELECT after CTE stripping) are needed because
    the CTE-stripped form is required for accurate FROM-clause parsing.

    :param normalised_sql: Lowercased, backtick-stripped, comment-free SQL used
                           for FROM/JOIN identifier extraction and CTE name
                           discovery.
    :param outer_sql: The core SELECT statement after CTE stripping; derived
                      from ``normalised_sql`` if not provided.
    :raises ValueError: If any table reference falls outside the configured
                        allowed pairs, or if a bare reference is ambiguous
                        because the table name exists in multiple allowed
                        datasets.
    """
    cfg           = get_cfg()
    allowed_pairs = cfg["ALLOWED_PAIRS"]

    # Lowercase pair set used for matching qualified references.
    allowed_pair_set: set[tuple[str, str]] = {
        (ds.lower(), tbl.lower()) for ds, tbl in allowed_pairs
    }
    # Mapping from lowercased table name to the set of allowed datasets that
    # contain it. Used to validate bare references and detect ambiguity.
    table_to_datasets: dict[str, set[str]] = {}
    for ds, tbl in allowed_pairs:
        table_to_datasets.setdefault(tbl.lower(), set()).add(ds.lower())

    cte_names: set[str] = set()
    cte_names.update(re.findall(r"\bwith\s+([\w]+)\s+as\s*\(", normalised_sql))
    cte_names.update(re.findall(r",\s*([\w]+)\s+as\s*\(",       normalised_sql))

    def _check(identifier: str) -> None:
        identifier_lc = identifier.lower()
        parts         = identifier_lc.split(".")
        bare          = parts[-1]
        if bare in cte_names:
            return
        if len(parts) == 1:
            # Bare reference — must be allowed AND unambiguous.
            if bare not in table_to_datasets:
                raise ValueError(
                    f"Policy Violation: Query references disallowed table "
                    f"'{identifier}'. Allowed: {_format_allowed_pairs(allowed_pairs)}."
                )
            if len(table_to_datasets[bare]) > 1:
                raise ValueError(
                    f"Policy Violation: Bare table reference '{identifier}' is "
                    f"ambiguous — it exists in multiple allowed datasets "
                    f"({sorted(table_to_datasets[bare])}). Use the fully-qualified "
                    "'dataset.table' form instead."
                )
        else:
            # Qualified — match on the last two segments (ignoring optional project).
            qual_ds  = parts[-2]
            qual_tbl = parts[-1]
            if (qual_ds, qual_tbl) not in allowed_pair_set:
                raise ValueError(
                    f"Policy Violation: Query references disallowed table "
                    f"'{identifier}'. Allowed: {_format_allowed_pairs(allowed_pairs)}."
                )

    # Check 1: explicit FROM / JOIN targets.
    for target in re.findall(
        r"\b(?:from|join)\s+(?!unnest\b|lateral\b)([\w.]+)", normalised_sql
    ):
        _check(target)

    # Check 2: comma-joined tables in the FROM clause.
    if outer_sql is None:
        outer_sql = _strip_cte(normalised_sql)

    from_match = re.search(
        r"\bfrom\s+(.+?)(?=\s+(?:join|where|group|order|having|limit|window)\b|$)",
        outer_sql,
        re.DOTALL,
    )
    if from_match:
        from_clause = from_match.group(1)
        prev = None
        while prev != from_clause:
            prev        = from_clause
            from_clause = re.sub(r"\([^()]*\)", "()", from_clause)
        for part in from_clause.split(","):
            part = part.strip()
            if part.startswith("("):
                continue
            token_match = re.match(r"([\w.]+)", part)
            if not token_match:
                continue
            identifier = token_match.group(1)
            if re.match(r"(?:unnest|lateral)\b", identifier):
                continue
            _check(identifier)


def validate_sql(sql: str) -> str:
    """
    Validate and sanitise an incoming SQL string against the server's policy.

    Performs the following checks in order: (1) rejects queries exceeding
    MAX_SQL_LEN characters; (2) strips all comment styles ("--", "#", "/* */")
    using sqlparse so comment-like content inside string literals is preserved;
    (3) normalises the query to lowercase and strips backtick quoting for
    validation-only analysis (the original-case clean SQL is what gets executed);
    (4) confirms the core statement is a SELECT (CTEs are allowed); (5) confirms
    the query targets at least one of the configured allowed tables in
    ALLOWED_PAIRS; (6) delegates to _assert_no_extra_tables to catch references
    to disallowed tables (including cross-dataset references, ambiguous bare
    names where a table appears in multiple allowed datasets, JOINs to
    unauthorised tables, and comma-joined tables in the FROM clause); and
    (7) rejects multi-statement queries by scanning for mid-query semicolons.

    :param sql: Raw SQL string received from the MCP client tool call.
    :returns: The original-case, backtick-normalised, comment-stripped SQL
              that is safe to pass to BigQuery for execution.
    :raises ValueError: For any policy violation, with a human-readable message
                        prefixed "Policy Violation: …".
    """
    cfg           = get_cfg()
    allowed_pairs = cfg["ALLOWED_PAIRS"]

    if len(sql) > cfg["MAX_SQL_LEN"]:
        raise ValueError(
            f"Policy Violation: SQL exceeds maximum length of {cfg['MAX_SQL_LEN']} characters."
        )

    clean = sqlparse.format(sql, strip_comments=True).strip()
    if not clean:
        raise ValueError("Policy Violation: Empty query after comment removal.")

    normalised = _remove_backtick_quotes(clean.lower())

    core = _strip_cte(normalised)
    if not core.strip().startswith("select"):
        raise ValueError("Policy Violation: Only SELECT statements are permitted.")

    # Require at least one FROM clause referencing an allowed table name. The
    # ``(?:[\w]+\.)*`` segment accepts optional dataset / project prefixes; the
    # alternation enumerates every allowed bare table name. Cross-dataset
    # qualified references (e.g. ``wrong_ds.allowed_table``) pass this gate but
    # are subsequently rejected by _assert_no_extra_tables.
    table_alt = "|".join(re.escape(tbl.lower()) for _, tbl in allowed_pairs)
    if not re.search(rf"\bfrom\s+(?:[\w]+\.)*(?:{table_alt})\b", normalised):
        raise ValueError(
            f"Policy Violation: Query must SELECT FROM one of the allowed tables: "
            f"{_format_allowed_pairs(allowed_pairs)}."
        )

    _assert_no_extra_tables(normalised, outer_sql=core)

    if ";" in clean.rstrip(";"):
        raise ValueError("Policy Violation: Multi-statement queries are prohibited.")

    return _remove_backtick_quotes(clean)


def _qualify_table(sql: str) -> str:
    """
    Rewrite bare allowed-table references to fully-qualified ``dataset.table`` form.

    Iterates over every allowed (dataset, table) pair and substitutes each bare
    occurrence of the table name (one not preceded by ``.`` or a word character)
    with the corresponding ``dataset.table``. Already-qualified references like
    ``dataset.table`` or ``project.dataset.table`` are left untouched because
    the negative lookbehind prevents matching after a dot. If the same table
    name appears in multiple allowed datasets the bare form is ambiguous and is
    left unqualified — validate_sql() rejects any such bare reference upstream,
    so this defensive skip only matters if validation is bypassed (e.g. in
    tests). Matching is case-insensitive but the replacement uses the configured
    table name's casing so the executed SQL preserves operator intent.

    :param sql: Comment-stripped, policy-validated SQL string.
    :returns: The same SQL with every unambiguous bare allowed-table reference
              expanded to ``dataset.table``.
    """
    cfg = get_cfg()

    # Group datasets by table name so we can detect ambiguity and skip it.
    table_to_datasets: dict[str, list[str]] = {}
    for ds, tbl in cfg["ALLOWED_PAIRS"]:
        table_to_datasets.setdefault(tbl, []).append(ds)

    result = sql
    for tbl, datasets in table_to_datasets.items():
        if len(datasets) != 1:
            # Ambiguous — validation rejects bare references to these tables,
            # so we leave any qualified occurrences alone.
            continue
        qualified = f"{datasets[0]}.{tbl}"
        escaped   = re.escape(tbl)
        result    = re.sub(
            rf"(?<![.\w]){escaped}\b",
            qualified,
            result,
            flags=re.IGNORECASE,
        )
    return result


# ---------------------------------------------------------------------------
# JSON serialisation
# ---------------------------------------------------------------------------
def _serialise(obj: Any) -> Any:
    """
    JSON default serialiser for BigQuery result types not handled by stdlib json.

    Called by json.dumps() for any value that the standard encoder cannot
    handle. Converts datetime and date objects to ISO-8601 strings (e.g.
    "2024-01-15T10:30:00") and Decimal values to Python floats so they round-
    trip cleanly as JSON numbers. Raises TypeError for any other unrecognised
    type so callers get an explicit error rather than silent data loss.

    :param obj: The Python object that json.dumps() could not serialise.
    :returns: A JSON-serialisable representation: ISO string for date/datetime,
              float for Decimal.
    :raises TypeError: If ``obj`` is not a handled type.
    """
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Type {type(obj).__name__} is not JSON serialisable")


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_table_schema() -> str:
    """
    MCP tool: return schema, partitioning, and clustering metadata for every
    configured allowed table as a JSON string.

    Acquires a slot on the meta semaphore via the non-blocking _try_acquire()
    before offloading the synchronous bq.fetch_schema() call to the
    _meta_executor thread pool, keeping the event loop free during the BigQuery
    RPCs. The schema is TTL-cached by BigQueryManager so most calls never leave
    the process. Returns a user-facing string on all error paths rather than
    propagating exceptions, so MCP clients receive a parseable text response.

    :returns: Pretty-printed JSON string with a single top-level "tables" array;
              each entry contains "dataset", "table", "partition_field",
              "clustering_fields", and "schema" keys on success. Returns a
              human-readable error message string if the semaphore is exhausted
              or the BigQuery RPC fails.
    """
    if _meta_sem is None:
        raise RuntimeError("_meta_sem not initialised — lifespan not running")

    if not await _try_acquire(_meta_sem):
        return "Service busy. Please retry in a moment."

    try:
        loop    = asyncio.get_running_loop()
        details = await loop.run_in_executor(_meta_executor, bq.fetch_schema)
        return json.dumps(details, indent=2)
    except Exception as e:
        _log("error", event="schema_fetch_failed", error=str(e))
        return "Schema Error: Unable to retrieve schema. Check server logs."
    finally:
        _meta_sem.release()


@mcp.tool()
async def query_assessments(sql: str) -> str:
    """
    MCP tool: execute a validated SELECT query against one of the configured
    allowed (dataset, table) pairs and return results as a JSON array string.

    The full execution pipeline is: rate-limit check → semaphore acquisition →
    SQL validation (validate_sql, multi-table aware) → table qualification
    (_qualify_table, multi-table aware) → dry-run cost estimate (cached via
    _DryRunCache) → scan-budget enforcement → live query execution with
    maximum_bytes_billed guard → row-cap truncation → JSON serialisation. All
    I/O-bound work runs in _query_executor to avoid blocking the event loop.
    Every error path returns a descriptive string rather than raising, so the
    MCP client always receives a well-formed text response. A unique request_id
    UUID is generated per call for log correlation.

    :param sql: Raw SQL string from the MCP tool call; must be a SELECT that
                targets one of the allowed (dataset, table) pairs. Cross-table
                JOINs between allowed tables are permitted; references to any
                other table are rejected.
    :returns: A JSON array string of row objects on success (e.g. "[{...},...]");
              or a descriptive error/policy-violation string on failure.
    """
    cfg        = get_cfg()
    request_id = str(uuid.uuid4())
    client_id  = _current_client.get()

    if not _rate_limiter.is_allowed(client_id):
        _log("warning", event="rate_limit_exceeded", request_id=request_id, client=client_id)
        return "Rate limit exceeded. Please wait before sending another query."

    _log("info", event="query_received", request_id=request_id, client=client_id)

    if _query_sem is None:
        raise RuntimeError("_query_sem not initialised — lifespan not running")

    if not await _try_acquire(_query_sem):
        _log("warning", event="query_sem_exhausted", request_id=request_id)
        return "Service busy. Please retry in a moment."

    try:
        validated_sql = validate_sql(sql)
        validated_sql = _qualify_table(validated_sql)

        if _dry_run_cache is None:
            raise RuntimeError("_dry_run_cache not initialised — lifespan not running")
        cache = _dry_run_cache

        loop = asyncio.get_running_loop()

        def _run() -> str:
            if bq.client is None:
                raise RuntimeError("BigQuery client not initialised")

            cached_bytes = cache.get(validated_sql)
            if cached_bytes is not None:
                bytes_est = cached_bytes
                _log("info", event="dry_run_cache_hit", request_id=request_id,
                     estimated_mb=round(bytes_est / (1024 * 1024), 2))
            else:
                dry_job   = bq.client.query(
                    validated_sql,
                    job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
                )
                bytes_est = dry_job.total_bytes_processed
                cache.set(validated_sql, bytes_est)

            if bytes_est > cfg["MAX_BYTES"]:
                mb = round(bytes_est / (1024 * 1024), 2)
                raise ValueError(
                    f"Scan limit exceeded: ~{mb}MB estimated, limit is {cfg['MAX_MB']}MB. "
                    "Add a filter on the target table's partition_field "
                    "(see get_table_schema for each table's partition column)."
                )

            _log("info", event="dry_run_passed", request_id=request_id,
                 estimated_mb=round(bytes_est / (1024 * 1024), 2))

            job  = bq.client.query(
                validated_sql,
                job_config=bigquery.QueryJobConfig(maximum_bytes_billed=cfg["MAX_BYTES"]),
            )
            rows = job.result(timeout=cfg["JOB_TIMEOUT"], page_size=500)

            buf   = io.StringIO()
            buf.write("[")
            count = 0
            for row in rows:
                if count >= cfg["MAX_ROWS"]:
                    _log("warning", event="row_cap_hit",
                         request_id=request_id, cap=cfg["MAX_ROWS"])
                    break
                if count > 0:
                    buf.write(",")
                buf.write(json.dumps(dict(row), default=_serialise))
                count += 1
            buf.write("]")

            _log("info", event="query_complete",
                 request_id=request_id, rows_returned=count)
            return buf.getvalue()

        result = await loop.run_in_executor(_query_executor, _run)
        return result

    except ValueError as ve:
        _log("warning", event="validation_error", request_id=request_id, error=str(ve))
        return str(ve)
    except FuturesTimeoutError:
        _log("error", event="query_timeout", request_id=request_id)
        return f"BigQuery Error: Query exceeded the {cfg['JOB_TIMEOUT']}s timeout."
    except gcp_exceptions.BadRequest as e:
        _log("error", event="bad_request", request_id=request_id, error=str(e))
        return (
            "BigQuery Error: The query contains invalid syntax or references unknown "
            "columns. Check column names, types, and filters against the schema."
        )
    except gcp_exceptions.Forbidden:
        _log("error", event="forbidden", request_id=request_id)
        return "BigQuery Error: Permission denied. Check the service account IAM roles."
    except Exception as e:
        _log("error", event="unexpected_error", request_id=request_id, error=str(e))
        return "BigQuery Error: An unexpected error occurred. Check server logs."
    finally:
        _query_sem.release()


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------

@app.post("/admin/invalidate-schema")
async def admin_invalidate_schema(request: Request):
    """
    Force-expire the schema cache so the next get_table_schema call fetches
    fresh metadata from BigQuery for every allowed table.

    Authentication is two-layered: _AuthMiddleware already validated the
    x-api-key header; this endpoint additionally requires a correct x-admin-key
    header validated via hmac.compare_digest() to prevent timing-based
    enumeration. A separate _admin_rate_limiter (ADMIN_RATE_LIMIT_QPM, default
    10 QPM) is applied before the key check so that brute-forcing the admin
    key via rapid requests — from a caller who already holds a valid MCP_API_KEY
    — is throttled independently of normal query traffic. On success, also runs
    cleanup() on both rate limiters to evict stale client buckets immediately.

    :returns: JSON {"status": "schema cache cleared"} with HTTP 200 on success;
              JSON {"detail": "Too many requests."} with HTTP 429 on rate limit;
              JSON {"detail": "Unauthorized"} with HTTP 403 on bad admin key.
    """
    cfg         = get_cfg()
    client_host = request.client.host if request.client else "unknown"

    if not _admin_rate_limiter.is_allowed(client_host):
        _log("warning", event="admin_rate_limit_exceeded", client=client_host)
        return JSONResponse({"detail": "Too many requests."}, status_code=429)

    admin_key = request.headers.get("x-admin-key", "").strip()
    try:
        key_ok = hmac.compare_digest(admin_key, cfg["ADMIN_KEY"])
    except Exception:
        key_ok = False

    if not key_ok:
        _log("warning", event="admin_unauthorised", client=client_host)
        return JSONResponse({"detail": "Unauthorized"}, status_code=403)

    bq.invalidate_schema_cache()
    _rate_limiter.cleanup()
    _admin_rate_limiter.cleanup()
    _log("info", event="admin_schema_invalidated", client=client_host)
    return {"status": "schema cache cleared"}


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    """
    Lightweight liveness probe endpoint for load balancers and orchestrators.

    Returns a static JSON payload immediately without touching the BigQuery
    client, semaphores, or any other shared resource. The /health path is
    explicitly exempted from authentication in _AuthMiddleware so that
    health probes can succeed before credentials are provisioned and without
    consuming rate-limit tokens.

    :returns: JSON {"status": "OK"} with HTTP 200.
    """
    return {"status": "OK"}


# ---------------------------------------------------------------------------
# Mount MCP
# ---------------------------------------------------------------------------
app.mount("/", mcp.streamable_http_app())
