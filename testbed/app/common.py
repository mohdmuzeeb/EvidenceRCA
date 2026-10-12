"""Pieces shared by the four services: app factory, downstream HTTP client, DB engine."""

import logging
import os
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.metrics import Observation
from sqlalchemy import Engine, create_engine

from app.telemetry import Telemetry

# Uninstrumented: liveness probes would otherwise dominate the telemetry.
EXCLUDED_PATHS = ("/health",)

log = logging.getLogger("evidencerca.common")


class DownstreamUnavailable(Exception):
    def __init__(self, service: str, reason: str):
        super().__init__(f"{service} unavailable: {reason}")
        self.service = service
        self.reason = reason


def create_app(service_name: str) -> tuple[FastAPI, Telemetry]:
    """Build an instrumented FastAPI app with request/latency/error metrics."""
    tel = Telemetry(service_name)
    HTTPXClientInstrumentor().instrument(
        tracer_provider=tel.tracer_provider, meter_provider=tel.meter_provider, raise_exception_on_conflict=True
    )

    requests_total = tel.meter.create_counter(
        "app.requests", unit="{request}", description="Requests handled, by route and status"
    )
    errors_total = tel.meter.create_counter(
        "app.errors", unit="{request}", description="Requests that ended with status >= 400"
    )
    duration = tel.meter.create_histogram(
        "app.request.duration", unit="ms", description="Server-side request latency"
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log.info("service started", extra={"event": "startup"})
        yield
        log.info("service stopping", extra={"event": "shutdown"})
        tel.shutdown()

    app = FastAPI(title=service_name, lifespan=lifespan)

    @app.middleware("http")
    async def record_request(request: Request, call_next):
        if request.url.path in EXCLUDED_PATHS:
            return await call_next(request)
        start = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        route = getattr(request.scope.get("route"), "path", "unmatched")
        attrs = {"http.route": route, "http.request.method": request.method,
                 "http.response.status_code": response.status_code}
        requests_total.add(1, attrs)
        duration.record(elapsed_ms, attrs)
        if response.status_code >= 400:
            kind = "server" if response.status_code >= 500 else "client"
            errors_total.add(1, {**attrs, "error.type": kind})
        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            response.headers["x-trace-id"] = format(ctx.trace_id, "032x")
        log.info(
            "request completed",
            extra={"http_route": route, "http_method": request.method,
                   "status_code": response.status_code, "duration_ms": round(elapsed_ms, 3)},
        )
        return response

    @app.exception_handler(DownstreamUnavailable)
    async def downstream_unavailable(request: Request, exc: DownstreamUnavailable):
        log.error("downstream unavailable", extra={"downstream": exc.service, "reason": exc.reason})
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.get("/health")
    def health():
        return {"status": "ok", "service": service_name}

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=tel.tracer_provider,
        meter_provider=tel.meter_provider,
        excluded_urls=",".join(EXCLUDED_PATHS),
        exclude_spans=["receive", "send"],
    )
    return app, tel


class Downstream:
    """HTTP client for one downstream service: timeout plus bounded retry on 5xx/transport errors."""

    def __init__(self, name: str, base_url: str):
        self.name = name
        self.client = httpx.Client(
            base_url=base_url, timeout=float(os.environ.get("DOWNSTREAM_TIMEOUT_S", "2.0"))
        )
        self.retries = int(os.environ.get("DOWNSTREAM_RETRIES", "1"))
        self.backoff_s = float(os.environ.get("DOWNSTREAM_BACKOFF_S", "0.1"))

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        for attempt in range(self.retries + 1):
            last = attempt == self.retries
            try:
                response = self.client.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                if last:
                    raise DownstreamUnavailable(self.name, type(exc).__name__) from exc
                reason = type(exc).__name__
            else:
                if response.status_code < 500 or last:
                    return response
                reason = f"http {response.status_code}"
            log.warning("retrying downstream call",
                        extra={"downstream": self.name, "path": path, "attempt": attempt + 1, "reason": reason})
            time.sleep(self.backoff_s * (attempt + 1))
        raise AssertionError("unreachable")


def create_db_engine(tel: Telemetry) -> Engine:
    """Small, bounded pool so that later phases can exhaust it realistically."""
    engine = create_engine(
        os.environ["DATABASE_URL"],
        pool_size=int(os.environ.get("DB_POOL_SIZE", "5")),
        max_overflow=0,
        pool_timeout=float(os.environ.get("DB_POOL_TIMEOUT_S", "2.0")),
        pool_pre_ping=True,
        # Shows up in Postgres connection logs, tying DB-side lines to the calling service.
        connect_args={"application_name": tel.service_name},
    )
    SQLAlchemyInstrumentor().instrument(
        engine=engine, tracer_provider=tel.tracer_provider, meter_provider=tel.meter_provider,
        raise_exception_on_conflict=True,
    )
    pool = engine.pool

    def observe(attr):
        return lambda options: [Observation(getattr(pool, attr)())]

    tel.meter.create_observable_gauge("app.db.pool.checked_out", [observe("checkedout")], unit="{connection}",
                                      description="Connections currently checked out of the pool")
    tel.meter.create_observable_gauge("app.db.pool.size", [observe("size")], unit="{connection}",
                                      description="Configured pool size")
    return engine
