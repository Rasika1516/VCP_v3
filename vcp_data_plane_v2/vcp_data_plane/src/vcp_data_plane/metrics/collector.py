"""Prometheus metrics collector for the VCP Data Plane worker."""
from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


class WorkerMetrics:
    """Per-worker Prometheus metrics."""

    def __init__(self, worker_id: str, registry: CollectorRegistry | None = None) -> None:
        labels = {"worker_id": worker_id}
        r = registry  # None = default global registry

        # Connection metrics
        self.vcps_total = Gauge(
            "vcp_worker_vcps_total",
            "Total number of VCPs managed by this worker",
            ["worker_id"],
            registry=r,
        ).labels(**labels)

        self.vcps_connected = Gauge(
            "vcp_worker_vcps_connected",
            "Number of VCPs with active WebSocket connections",
            ["worker_id"],
            registry=r,
        ).labels(**labels)

        self.connections_established = Counter(
            "vcp_worker_connections_established_total",
            "Total WebSocket connections successfully established",
            ["worker_id"],
            registry=r,
        ).labels(**labels)

        self.connections_failed = Counter(
            "vcp_worker_connections_failed_total",
            "Total WebSocket connection attempts that failed",
            ["worker_id"],
            registry=r,
        ).labels(**labels)

        self.reconnect_attempts = Counter(
            "vcp_worker_reconnect_attempts_total",
            "Total WebSocket reconnect attempts",
            ["worker_id"],
            registry=r,
        ).labels(**labels)

        # OCPP message metrics
        self.messages_sent = Counter(
            "vcp_worker_ocpp_messages_sent_total",
            "Total OCPP messages sent to CSMS",
            ["worker_id", "action"],
            registry=r,
        )
        self._worker_id = worker_id

        self.messages_received = Counter(
            "vcp_worker_ocpp_messages_received_total",
            "Total OCPP messages received from CSMS",
            ["worker_id", "action"],
            registry=r,
        )

        self.message_errors = Counter(
            "vcp_worker_ocpp_message_errors_total",
            "Total OCPP messages that resulted in an error",
            ["worker_id"],
            registry=r,
        ).labels(**labels)

        self.message_latency = Histogram(
            "vcp_worker_ocpp_message_latency_seconds",
            "Round-trip latency for CALL/CALLRESULT pairs",
            ["worker_id", "action"],
            buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0],
            registry=r,
        )

        # Transaction metrics
        self.transactions_started = Counter(
            "vcp_worker_transactions_started_total",
            "Total charging transactions started",
            ["worker_id"],
            registry=r,
        ).labels(**labels)

        self.transactions_stopped = Counter(
            "vcp_worker_transactions_stopped_total",
            "Total charging transactions stopped",
            ["worker_id"],
            registry=r,
        ).labels(**labels)

        self.active_transactions = Gauge(
            "vcp_worker_active_transactions",
            "Number of VCPs currently in a charging transaction",
            ["worker_id"],
            registry=r,
        ).labels(**labels)

        # VCP state distribution
        self.vcps_by_state = Gauge(
            "vcp_worker_vcps_by_state",
            "Number of VCPs in each state",
            ["worker_id", "state"],
            registry=r,
        )

        # Fault injection
        self.faults_injected = Counter(
            "vcp_worker_faults_injected_total",
            "Total fault injections triggered",
            ["worker_id", "fault_type"],
            registry=r,
        )

        # Command metrics
        self.commands_received = Counter(
            "vcp_worker_commands_received_total",
            "Total control commands received via Redis Streams",
            ["worker_id", "command_type"],
            registry=r,
        )

        # Heartbeat counter
        self.heartbeats_sent = Counter(
            "vcp_worker_heartbeats_sent_total",
            "Total heartbeat messages sent",
            ["worker_id"],
            registry=r,
        ).labels(**labels)

    def on_heartbeat(self, vcp_id: str) -> None:
        self.heartbeats_sent.inc()

    def on_transaction_started(self, vcp_id: str) -> None:
        self.transactions_started.inc()
        self.active_transactions.inc()

    def on_transaction_stopped(self, vcp_id: str) -> None:
        self.transactions_stopped.inc()
        self.active_transactions.dec()

    def on_message_sent(self, action: str) -> None:
        self.messages_sent.labels(worker_id=self._worker_id, action=action).inc()

    def on_message_received(self, action: str) -> None:
        self.messages_received.labels(worker_id=self._worker_id, action=action).inc()

    def on_fault_injected(self, fault_type: str) -> None:
        self.faults_injected.labels(worker_id=self._worker_id, fault_type=fault_type).inc()

    def on_command_received(self, command_type: str) -> None:
        self.commands_received.labels(worker_id=self._worker_id, command_type=command_type).inc()

    def update_state_distribution(self, state_counts: dict[str, int]) -> None:
        for state, count in state_counts.items():
            self.vcps_by_state.labels(worker_id=self._worker_id, state=state).set(count)
