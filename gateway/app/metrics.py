"""Prometheus metrics, exposed on /metrics.

Labels are limited to configured model aliases and backends, so the number of
time series stays bounded. Per-key numbers live in the usage log instead.
"""

from collections.abc import Iterable

from prometheus_client import CollectorRegistry, Counter, Histogram
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from app.config import RoutesConfig
from app.routing import Cooldown, Hook, RequestOutcome

# Response times of LLMs range from milliseconds (errors, cached prefixes) to minutes.
DURATION_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)
NONE = "none"  # label value when no backend answered


class CooldownCollector(Collector):
    """Reports at scrape time which deployments are in cooldown (1) or not (0)."""

    def __init__(self, routes: RoutesConfig):
        self.deployments = sorted(
            {(dep.backend, dep.model) for deps in routes.models.values() for dep in deps}
        )
        self.cooldown: Cooldown | None = None  # set once the router exists

    def collect(self) -> Iterable[GaugeMetricFamily]:
        gauge = GaugeMetricFamily(
            "gateway_deployment_cooldown",
            "Whether a deployment is in cooldown after a recent failure (1) or not (0)",
            labels=["backend", "upstream_model"],
        )
        cooling = self.cooldown.cooling() if self.cooldown else set()
        for backend, model in self.deployments:
            gauge.add_metric([backend, model], 1 if (backend, model) in cooling else 0)
        yield gauge


class Metrics:
    def __init__(self, routes: RoutesConfig):
        self.models = set(routes.models)
        self.registry = CollectorRegistry()
        self._cooldown = CooldownCollector(routes)
        self.registry.register(self._cooldown)

        self.requests = Counter(
            "gateway_requests",
            "Chat completion requests by model alias, serving backend and outcome",
            ["model", "backend", "status"],
            registry=self.registry,
        )
        self.duration = Histogram(
            "gateway_request_duration_seconds",
            "Time from receiving a request until its response (or stream) is complete",
            ["model", "backend"],
            buckets=DURATION_BUCKETS,
            registry=self.registry,
        )
        self.first_byte = Histogram(
            "gateway_time_to_first_byte_seconds",
            "Streaming only: time until the first chunk from the backend",
            ["model", "backend"],
            buckets=DURATION_BUCKETS,
            registry=self.registry,
        )
        self.tokens = Counter(
            "gateway_tokens",
            "Tokens processed, as reported by the backends",
            ["model", "backend", "type"],
            registry=self.registry,
        )
        self.fallbacks = Counter(
            "gateway_fallbacks",
            "Failed deployment attempts, by the backend that failed and the one that "
            f"finally answered ({NONE} if none did)",
            ["model", "from_backend", "to_backend"],
            registry=self.registry,
        )
        self.policy_denials = Counter(
            "gateway_policy_denials",
            "Requests rejected because the key may not use external models",
            ["model"],
            registry=self.registry,
        )

    def watch_cooldown(self, cooldown: Cooldown) -> None:
        self._cooldown.cooldown = cooldown

    def hook(self) -> Hook:
        async def record_metrics(outcome: RequestOutcome) -> None:
            self.observe(outcome)

        return record_metrics

    def observe(self, outcome: RequestOutcome) -> None:
        # Unknown aliases come from client input; don't let them create time series.
        model = outcome.requested_model if outcome.requested_model in self.models else "unknown"
        backend = outcome.backend or NONE

        self.requests.labels(model, backend, outcome.status).inc()
        self.duration.labels(model, backend).observe(outcome.latency_s)
        if outcome.ttfb_s is not None:
            self.first_byte.labels(model, backend).observe(outcome.ttfb_s)
        if outcome.prompt_tokens:
            self.tokens.labels(model, backend, "prompt").inc(outcome.prompt_tokens)
        if outcome.completion_tokens:
            self.tokens.labels(model, backend, "completion").inc(outcome.completion_tokens)
        for failed in outcome.failed_backends:
            self.fallbacks.labels(model, failed, backend).inc()
        if outcome.error_code == "external_models_not_allowed":
            self.policy_denials.labels(model).inc()
