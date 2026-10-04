#!/usr/bin/env python3
"""
Telemetry Capture Engine for ObservaSage.
Given a run ID and time window [t_start, t_end]:
1. Pulls Docker container logs
2. Queries Prometheus PromQL API for metric time series
3. Queries Jaeger REST API for error traces & spans
Saves a unified run snapshot to data/runs/<status>/<run_id>_telemetry.json
"""

import sys
import os
import json
import argparse
import datetime
import requests
import docker
from rich.console import Console

console = Console()

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
JAEGER_URL = os.getenv("JAEGER_URL", "http://localhost:16686")
RUNS_DIR = os.getenv("DATA_RUNS_DIR", "data/runs")

MONITORED_SERVICES = [
    "cartservice",
    "paymentservice",
    "currencyservice",
    "checkoutservice",
    "frontend",
    "productcatalogservice",
    "shippingservice",
    "emailservice",
    "quoteservice",
]

# OTel demo containers register in Jaeger as 'unknown_service:<name>' or just '<name>'.
# We query both variants to be resilient to different SDK configurations.
def _jaeger_service_variants(container_name: str) -> list[str]:
    return [
        container_name,
        f"unknown_service:{container_name}",
        f"unknown_service:node",  # JS services (frontend, emailservice) show as this
    ]

def collect_logs(client, services, start_time: datetime.datetime, end_time: datetime.datetime):
    logs_by_service = {}
    since_ts = int(start_time.timestamp())
    until_ts = int(end_time.timestamp()) + 5  # buffer 5 seconds

    for svc in services:
        try:
            container = client.containers.get(svc)
            raw = container.logs(since=since_ts, until=until_ts, timestamps=True).decode("utf-8", errors="replace")
            logs_by_service[svc] = [line.strip() for line in raw.split("\n") if line.strip()]
        except Exception as e:
            logs_by_service[svc] = [f"[COLLECTION_ERROR] {e}"]
    return logs_by_service

def collect_metrics(services, start_time: datetime.datetime, end_time: datetime.datetime):
    metrics_results = []
    # Widen the window slightly so short runs still have data
    t_start_iso = (start_time - datetime.timedelta(seconds=60)).isoformat()
    t_end_iso = (end_time + datetime.timedelta(seconds=30)).isoformat()

    # cAdvisor metric names — container_label_com_docker_compose_service filters by service name
    prom_queries = [
        (
            "cpu_usage_rate",
            'sum(rate(container_cpu_usage_seconds_total{container_label_com_docker_compose_service!=""}[30s])) '
            'by (container_label_com_docker_compose_service)',
        ),
        (
            "memory_rss",
            'sum(container_memory_rss{container_label_com_docker_compose_service!=""}) '
            'by (container_label_com_docker_compose_service)',
        ),
        (
            "memory_usage",
            'sum(container_memory_usage_bytes{container_label_com_docker_compose_service!=""}) '
            'by (container_label_com_docker_compose_service)',
        ),
        (
            "memory_limit",
            'sum(container_spec_memory_limit_bytes{container_label_com_docker_compose_service!=""}) '
            'by (container_label_com_docker_compose_service)',
        ),
        (
            "network_rx_bytes",
            'sum(rate(container_network_receive_bytes_total{container_label_com_docker_compose_service!=""}[30s])) '
            'by (container_label_com_docker_compose_service)',
        ),
        (
            "network_tx_bytes",
            'sum(rate(container_network_transmit_bytes_total{container_label_com_docker_compose_service!=""}[30s])) '
            'by (container_label_com_docker_compose_service)',
        ),
    ]

    for metric_name, query in prom_queries:
        try:
            resp = requests.get(
                f"{PROMETHEUS_URL}/api/v1/query_range",
                params={"query": query, "start": t_start_iso, "end": t_end_iso, "step": "5s"},
                timeout=5.0
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {}).get("result", [])
                metrics_results.append({"metric_name": metric_name, "query": query, "data": data})
            else:
                metrics_results.append({"metric_name": metric_name, "query": query,
                                        "error": f"HTTP {resp.status_code}", "data": []})
        except Exception as e:
            metrics_results.append({"metric_name": metric_name, "query": query,
                                    "error": str(e), "data": []})

    return metrics_results

def collect_traces(services, start_time: datetime.datetime, end_time: datetime.datetime):
    """Query Jaeger for traces. Jaeger indexes by OTel service.name (not container name).
    We try both the container name and common OTel demo service name patterns."""
    traces_results = []
    # Widen window: runs can be very short
    start_us = int((start_time - datetime.timedelta(seconds=30)).timestamp() * 1_000_000)
    end_us = int((end_time + datetime.timedelta(seconds=30)).timestamp() * 1_000_000)

    # First, discover what service names are actually registered in Jaeger
    try:
        svc_resp = requests.get(f"{JAEGER_URL}/api/services", timeout=5.0)
        jaeger_services = svc_resp.json().get("data", []) if svc_resp.status_code == 200 else []
    except Exception:
        jaeger_services = []

    # Build query targets: match container names against registered Jaeger service names
    query_targets = set()
    for svc in services:
        for variant in _jaeger_service_variants(svc):
            if variant in jaeger_services:
                query_targets.add(variant)
    # Also query unknown_service:node once (covers JS/Node services)
    if "unknown_service:node" in jaeger_services:
        query_targets.add("unknown_service:node")
    # Fallback: query all registered services if no matches found
    if not query_targets:
        query_targets = set(jaeger_services)

    seen_trace_ids = set()
    for svc in query_targets:
        try:
            resp = requests.get(
                f"{JAEGER_URL}/api/traces",
                params={
                    "service": svc,
                    "start": start_us,
                    "end": end_us,
                    "limit": 50,
                    "lookback": "custom",
                },
                timeout=5.0
            )
            if resp.status_code == 200:
                traces = resp.json().get("data", [])
                for trace in traces:
                    tid = trace.get("traceID", "")
                    if tid not in seen_trace_ids:
                        seen_trace_ids.add(tid)
                        traces_results.append(trace)
            elif resp.status_code != 404:
                console.print(f"[dim]Jaeger returned {resp.status_code} for {svc}[/dim]")
        except Exception as e:
            console.print(f"[dim]Jaeger collection error for {svc}: {e}[/dim]")

    return traces_results

def main():
    parser = argparse.ArgumentParser(description="ObservaSage Telemetry Capture Engine")
    parser.add_argument("--meta-file", type=str, required=True, help="Path to run metadata JSON file")
    args = parser.parse_args()

    if not os.path.exists(args.meta_file):
        console.print(f"[bold red]Metadata file not found: {args.meta_file}[/bold red]")
        sys.exit(1)

    with open(args.meta_file, "r") as f:
        meta = json.load(f)

    run_id = meta["run_id"]
    status = meta["status"]
    t_start = datetime.datetime.fromisoformat(meta["start_time"])
    t_end = datetime.datetime.fromisoformat(meta["end_time"])

    console.print(f"[bold cyan]Capturing Telemetry for Run: {run_id} ({status})...[/bold cyan]")

    client = docker.from_env()
    logs = collect_logs(client, MONITORED_SERVICES, t_start, t_end)
    metrics = collect_metrics(MONITORED_SERVICES, t_start, t_end)
    traces = collect_traces(MONITORED_SERVICES, t_start, t_end)

    telemetry_payload = {
        "run_id": run_id,
        "metadata": meta,
        "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "telemetry": {
            "logs": logs,
            "metrics": metrics,
            "traces": traces
        }
    }

    subfolder = "success" if status == "SUCCESS" else "failed"
    out_path = os.path.join(RUNS_DIR, subfolder, f"{run_id}_telemetry.json")
    with open(out_path, "w") as f:
        json.dump(telemetry_payload, f, indent=2)

    console.print(f"[bold green]✔ Captured {len(logs)} service logs, {len(metrics)} metric series, and {len(traces)} traces.[/bold green]")
    console.print(f"[bold green]Saved to: {out_path}[/bold green]\n")

if __name__ == "__main__":
    main()
