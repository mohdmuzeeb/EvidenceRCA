"""OpenTelemetry setup shared by every testbed service.

Each service exports traces, metrics and logs over OTLP/HTTP to the collector
(endpoint from OTEL_EXPORTER_OTLP_ENDPOINT) and also writes one JSON object per
log line to stdout. Both log paths carry the active trace_id/span_id.
"""

import json
import logging
import os
import socket
import sys
import time
from datetime import datetime, timezone

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

SERVICE_VERSION = "0.1.0"

# Process-level metrics only; host (system.*) metrics come from the collector's
# docker_stats receiver at container granularity.
PROCESS_METRICS = {
    "process.cpu.time": ["user", "system"],
    "process.cpu.utilization": None,
    "process.memory.usage": None,
    "process.memory.virtual": None,
    "process.thread.count": None,
    "process.open_file_descriptor.count": None,
    "process.runtime.gc_count": None,
}

_STANDARD_RECORD_KEYS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with trace context and any `extra=` fields."""

    def __init__(self, service_name: str):
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        ctx = trace.get_current_span().get_span_context()
        doc = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="microseconds"),
            "level": record.levelname,
            "service": self.service_name,
            "logger": record.name,
            "msg": record.getMessage(),
            "trace_id": format(ctx.trace_id, "032x") if ctx.is_valid else None,
            "span_id": format(ctx.span_id, "016x") if ctx.is_valid else None,
        }
        doc.update({k: v for k, v in vars(record).items() if k not in _STANDARD_RECORD_KEYS})
        if record.exc_info:
            doc["exception"] = self.formatException(record.exc_info)
        return json.dumps(doc, default=str)


class Telemetry:
    """Owns the three SDK providers so they can be flushed on shutdown."""

    def __init__(self, service_name: str):
        self.service_name = service_name
        self.resource = Resource.create(
            {
                "service.name": service_name,
                "service.namespace": "evidencerca-testbed",
                "service.version": SERVICE_VERSION,
                "service.instance.id": socket.gethostname(),
                "deployment.environment.name": "testbed",
            }
        )

        self.tracer_provider = TracerProvider(resource=self.resource)
        self.tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(self.tracer_provider)

        interval_ms = int(os.environ.get("OTEL_METRIC_EXPORT_INTERVAL", "5000"))
        reader = PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=interval_ms)
        self.meter_provider = MeterProvider(resource=self.resource, metric_readers=[reader])
        metrics.set_meter_provider(self.meter_provider)
        SystemMetricsInstrumentor(config=PROCESS_METRICS).instrument(
            meter_provider=self.meter_provider, raise_exception_on_conflict=True
        )

        self.logger_provider = LoggerProvider(resource=self.resource)
        self.logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
        set_logger_provider(self.logger_provider)

        root = logging.getLogger()
        root.setLevel(logging.INFO)
        stdout = logging.StreamHandler(sys.stdout)
        stdout.setFormatter(JsonFormatter(service_name))
        root.addHandler(stdout)
        root.addHandler(LoggingHandler(level=logging.INFO, logger_provider=self.logger_provider))
        # Keep exporter/HTTP-client internals out of the exported logs.
        for noisy in ("urllib3", "httpx", "httpcore", "opentelemetry"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

        self.tracer = trace.get_tracer(f"evidencerca.{service_name}", SERVICE_VERSION)
        self.meter = metrics.get_meter(f"evidencerca.{service_name}", SERVICE_VERSION)
        self.started_at = time.time()

    def shutdown(self) -> None:
        """Flush and stop all providers (called from the app lifespan)."""
        for provider in (self.tracer_provider, self.logger_provider, self.meter_provider):
            provider.shutdown()
