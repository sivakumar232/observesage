#!/usr/bin/env python3
"""
ObservaSage Fault Injection Engine
====================================
Injects real, reproducible failures into the running Docker stack.

Usage:
    uv run python scripts/inject_fault.py --list
    uv run python scripts/inject_fault.py --scenario <name>
    uv run python scripts/inject_fault.py --scenario clear

Fault Scenarios by Edge Case Category:

  EC-1  OOM / Resource Exhaustion     → oom_cart, oom_checkout, oom_payment,
                                         cpu_throttle_cart, cpu_throttle_checkout
  EC-2  Flaky / Intermittent Latency  → latency_payment, latency_cart,
                                         latency_currency, flag_load_spike
  EC-3  Cascading Service Crash       → crash_payment, crash_currency, crash_redis,
                                         crash_shipping, crash_email, crash_checkout,
                                         cascade_payment_currency
  EC-4  Silent Data Corruption        → redis_flush, redis_corrupt_cart,
                                         flag_cart_failure, flag_product_failure
  EC-5  Infrastructure Drift          → net_partition_cart, net_partition_payment,
                                         restart_loop_checkout, flag_load_spike

  UTIL  clear                         → Restore all containers and flags to healthy defaults
"""

import sys
import time
import json
import argparse
import subprocess
import requests
import docker
from rich.console import Console
from rich.table import Table
from rich import box

console = Console()

FLAGD_URL = "http://localhost:8013"
FLAGD_CONFIG_PATH = "config/demo.flagd.json"

# ──────────────────────────────────────────────
#  All injectable fault scenarios
# ──────────────────────────────────────────────
SCENARIOS: dict[str, dict] = {
    # ── EC-1: OOM / Resource Exhaustion ──────────────────────────
    "oom_cart": {
        "edge_case": "EC-1",
        "description": "Restrict cartservice RAM to 25MB → Linux OOM-killer fires, exit code 137",
        "method": "docker_mem_limit",
        "target": "cartservice",
        "mem_limit": "25m",
    },
    "oom_checkout": {
        "edge_case": "EC-1",
        "description": "Restrict checkoutservice RAM to 30MB → OOM kill during checkout flow",
        "method": "docker_mem_limit",
        "target": "checkoutservice",
        "mem_limit": "30m",
    },
    "oom_payment": {
        "edge_case": "EC-1",
        "description": "Restrict paymentservice RAM to 20MB → OOM kill mid-payment",
        "method": "docker_mem_limit",
        "target": "paymentservice",
        "mem_limit": "20m",
    },
    "oom_currency": {
        "edge_case": "EC-1",
        "description": "Restrict currencyservice RAM to 20MB → OOM kill on currency conversion",
        "method": "docker_mem_limit",
        "target": "currencyservice",
        "mem_limit": "20m",
    },
    "cpu_throttle_cart": {
        "edge_case": "EC-1",
        "description": "Cap cartservice CPU to 5% quota → CFS throttling, requests crawl",
        "method": "docker_cpu_limit",
        "target": "cartservice",
        "cpu_quota": 5000,   # microseconds per 100ms period → 5%
        "cpu_period": 100000,
    },
    "cpu_throttle_checkout": {
        "edge_case": "EC-1",
        "description": "Cap checkoutservice CPU to 5% → orchestration bottleneck",
        "method": "docker_cpu_limit",
        "target": "checkoutservice",
        "cpu_quota": 5000,
        "cpu_period": 100000,
    },

    # ── EC-2: Flaky / Intermittent Latency ───────────────────────
    "latency_payment": {
        "edge_case": "EC-2",
        "description": "Add 3s artificial delay to paymentservice via tc netem → checkout timeouts",
        "method": "tc_netem",
        "target": "paymentservice",
        "delay_ms": 3000,
        "jitter_ms": 500,
    },
    "latency_cart": {
        "edge_case": "EC-2",
        "description": "Add 2s delay to cartservice → add-to-cart timeouts from frontend",
        "method": "tc_netem",
        "target": "cartservice",
        "delay_ms": 2000,
        "jitter_ms": 300,
    },
    "latency_currency": {
        "edge_case": "EC-2",
        "description": "Add 2.5s delay to currencyservice → price conversion timeouts",
        "method": "tc_netem",
        "target": "currencyservice",
        "delay_ms": 2500,
        "jitter_ms": 200,
    },
    "packet_loss_payment": {
        "edge_case": "EC-2",
        "description": "Drop 40% of packets from paymentservice → intermittent failures",
        "method": "tc_netem",
        "target": "paymentservice",
        "delay_ms": 0,
        "loss_pct": 40,
    },
    "flag_load_spike": {
        "edge_case": "EC-2",
        "description": "Enable load generator flood flag → thundering herd, cascading timeouts",
        "method": "flagd",
        "flag": "loadgeneratorFloodHomepage",
        "value": "on",
    },

    # ── EC-3: Cascading Service Crash ─────────────────────────────
    "crash_payment": {
        "edge_case": "EC-3",
        "description": "Stop paymentservice → checkoutservice fails with cascade errors",
        "method": "docker_stop",
        "target": "paymentservice",
    },
    "crash_currency": {
        "edge_case": "EC-3",
        "description": "Stop currencyservice → checkout fails silently across all calls",
        "method": "docker_stop",
        "target": "currencyservice",
    },
    "crash_redis": {
        "edge_case": "EC-3",
        "description": "Stop redis-cart → cartservice loses all cart data, cascade to checkout",
        "method": "docker_stop",
        "target": "redis-cart",
    },
    "crash_shipping": {
        "edge_case": "EC-3",
        "description": "Stop shippingservice → checkout silently fails at shipping cost step",
        "method": "docker_stop",
        "target": "shippingservice",
    },
    "crash_email": {
        "edge_case": "EC-3",
        "description": "Stop emailservice → order confirmation step fails in checkout flow",
        "method": "docker_stop",
        "target": "emailservice",
    },
    "crash_checkout": {
        "edge_case": "EC-3",
        "description": "Stop checkoutservice → frontend gets 503, all checkout attempts fail",
        "method": "docker_stop",
        "target": "checkoutservice",
    },
    "crash_productcatalog": {
        "edge_case": "EC-3",
        "description": "Stop productcatalogservice → product page and cart both break",
        "method": "docker_stop",
        "target": "productcatalogservice",
    },
    "cascade_payment_currency": {
        "edge_case": "EC-3",
        "description": "Stop both payment + currency → multi-hop cascade, logs misleading",
        "method": "docker_stop_multi",
        "targets": ["paymentservice", "currencyservice"],
    },
    "flag_payment_failure": {
        "edge_case": "EC-3",
        "description": "Enable paymentServiceFailure flag → payment returns error mid-flow",
        "method": "flagd",
        "flag": "paymentServiceFailure",
        "value": "on",
    },
    "flag_shipping_failure": {
        "edge_case": "EC-3",
        "description": "Enable shippingServiceFailure flag → shipping cost calculation fails",
        "method": "flagd",
        "flag": "shippingServiceFailure",
        "value": "on",
    },
    "restart_loop_checkout": {
        "edge_case": "EC-3",
        "description": "Rapidly restart checkoutservice 5 times → CrashLoopBackOff simulation",
        "method": "restart_loop",
        "target": "checkoutservice",
        "cycles": 5,
        "interval_s": 4,
    },

    # ── EC-4: Silent Data Corruption ──────────────────────────────
    "redis_flush": {
        "edge_case": "EC-4",
        "description": "FLUSHALL on redis-cart → all carts become empty, checkout exits 0 but ships empty order",
        "method": "redis_cmd",
        "cmd": "FLUSHALL",
    },
    "redis_corrupt_cart": {
        "edge_case": "EC-4",
        "description": "Inject malformed protobuf data into a cart key → cart deserialisation error with exit 0",
        "method": "redis_cmd",
        "cmd": "SET",
        "args": ["cart:corrupt_user_99", "THIS_IS_NOT_VALID_PROTOBUF_DATA_XYZ"],
    },
    "flag_cart_failure": {
        "edge_case": "EC-4",
        "description": "Enable cartServiceFailure flag → cart silently returns empty response (exit 0, wrong data)",
        "method": "flagd",
        "flag": "cartServiceFailure",
        "value": "on",
    },
    "flag_product_failure": {
        "edge_case": "EC-4",
        "description": "Enable productCatalogFailure flag → product info silently wrong",
        "method": "flagd",
        "flag": "productCatalogFailure",
        "value": "on",
    },

    # ── EC-5: Infrastructure Drift ────────────────────────────────
    "net_partition_cart": {
        "edge_case": "EC-5",
        "description": "Disconnect cartservice from observasage-net → network partition, vague timeouts in logs",
        "method": "docker_net_disconnect",
        "target": "cartservice",
        "network": "final_year_project_observasage-net",
    },
    "net_partition_payment": {
        "edge_case": "EC-5",
        "description": "Disconnect paymentservice from network → full infra-level partition",
        "method": "docker_net_disconnect",
        "target": "paymentservice",
        "network": "final_year_project_observasage-net",
    },

    # ── UTIL: Clear / Restore ─────────────────────────────────────
    "clear": {
        "edge_case": "UTIL",
        "description": "Restore ALL containers, memory limits, network, and feature flags to healthy defaults",
        "method": "clear_all",
    },
}

# ──────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────

def get_docker_client() -> docker.DockerClient:
    try:
        return docker.from_env()
    except Exception as e:
        console.print(f"[bold red]Docker error:[/bold red] {e}")
        sys.exit(1)


def _docker_mem_limit(client: docker.DockerClient, target: str, mem_limit: str) -> None:
    container = client.containers.get(target)
    container.update(mem_limit=mem_limit, memswap_limit=mem_limit)
    console.print(f"  [yellow]Memory restricted:[/yellow] {target} → {mem_limit}")


def _docker_cpu_limit(client: docker.DockerClient, target: str, cpu_quota: int, cpu_period: int) -> None:
    container = client.containers.get(target)
    container.update(cpu_quota=cpu_quota, cpu_period=cpu_period)
    console.print(f"  [yellow]CPU throttled:[/yellow] {target} → {cpu_quota}/{cpu_period}µs ({cpu_quota/cpu_period*100:.1f}%)")


def _docker_stop(client: docker.DockerClient, target: str) -> None:
    container = client.containers.get(target)
    container.stop(timeout=1)
    console.print(f"  [red]Stopped:[/red] {target}")


def _docker_stop_multi(client: docker.DockerClient, targets: list[str]) -> None:
    for t in targets:
        _docker_stop(client, t)


def _tc_netem(target: str, delay_ms: int, jitter_ms: int = 0, loss_pct: int = 0) -> None:
    """Inject network delay/loss inside a container using tc netem."""
    try:
        parts = ["tc", "qdisc", "add", "dev", "eth0", "root", "netem"]
        if delay_ms > 0:
            parts += ["delay", f"{delay_ms}ms", f"{jitter_ms}ms"]
        if loss_pct > 0:
            parts += ["loss", f"{loss_pct}%"]
        result = subprocess.run(
            ["docker", "exec", "--privileged", target] + parts,
            capture_output=True, text=True
        )
        if result.returncode != 0:
            # Already exists — replace
            parts[3] = "change"
            subprocess.run(["docker", "exec", "--privileged", target] + parts,
                           capture_output=True, text=True)
        desc = f"{delay_ms}ms delay" if delay_ms else ""
        desc += f" {loss_pct}% packet loss" if loss_pct else ""
        console.print(f"  [yellow]Network degraded:[/yellow] {target} → {desc.strip()}")
    except Exception as e:
        console.print(f"  [dim]tc netem not available (container may not have iproute2): {e}[/dim]")
        console.print(f"  [dim]Tip: Add 'iproute2' to container image or use flagd-based faults instead.[/dim]")


def _clear_tc_netem(target: str) -> None:
    subprocess.run(
        ["docker", "exec", "--privileged", target, "tc", "qdisc", "del", "dev", "eth0", "root"],
        capture_output=True, text=True
    )


def _flagd_set(flag: str, variant: str) -> None:
    """Write flag variant to flagd config file — flagd hot-reloads on file change."""
    try:
        with open(FLAGD_CONFIG_PATH, "r") as f:
            cfg = json.load(f)
        if flag in cfg["flags"]:
            cfg["flags"][flag]["defaultVariant"] = variant
        with open(FLAGD_CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
        console.print(f"  [yellow]Flag set:[/yellow] {flag} → {variant}")
    except Exception as e:
        console.print(f"  [red]flagd error:[/red] {e}")


def _redis_cmd(client: docker.DockerClient, cmd: str, args: list[str] | None = None) -> None:
    try:
        redis_container = client.containers.get("redis-cart")
        full_cmd = ["redis-cli"] + [cmd] + (args or [])
        result = redis_container.exec_run(full_cmd)
        output = result.output.decode().strip()
        console.print(f"  [yellow]Redis {cmd}:[/yellow] {output}")
    except Exception as e:
        console.print(f"  [red]Redis error:[/red] {e}")


def _docker_net_disconnect(client: docker.DockerClient, target: str, network: str) -> None:
    try:
        net = client.networks.get(network)
        net.disconnect(target, force=True)
        console.print(f"  [red]Network disconnected:[/red] {target} ↔ {network}")
    except Exception as e:
        console.print(f"  [red]Network disconnect error:[/red] {e}")


def _docker_net_reconnect(client: docker.DockerClient, target: str, network: str) -> None:
    try:
        net = client.networks.get(network)
        net.connect(target)
        console.print(f"  [green]Network reconnected:[/green] {target} ↔ {network}")
    except Exception as e:
        console.print(f"  [dim]Could not reconnect {target}: {e}[/dim]")


def _restart_loop(client: docker.DockerClient, target: str, cycles: int, interval_s: int) -> None:
    for i in range(cycles):
        container = client.containers.get(target)
        container.restart(timeout=1)
        console.print(f"  [yellow]Restart cycle {i+1}/{cycles}:[/yellow] {target}")
        if i < cycles - 1:
            time.sleep(interval_s)


def _clear_all(client: docker.DockerClient) -> None:
    """Restore everything to clean state."""
    RESTORABLE = [
        "cartservice", "checkoutservice", "paymentservice", "currencyservice",
        "shippingservice", "emailservice", "redis-cart", "productcatalogservice", "frontend",
    ]
    NETWORK = "final_year_project_observasage-net"

    console.print("\n[cyan]Restoring container resource limits...[/cyan]")
    for name in RESTORABLE:
        try:
            c = client.containers.get(name)
            c.update(mem_limit=0, memswap_limit=0, cpu_quota=-1, cpu_period=0)
            if c.status != "running":
                c.start()
                console.print(f"  [green]Restarted:[/green] {name}")
            else:
                console.print(f"  [green]Limits cleared:[/green] {name}")
        except Exception as e:
            console.print(f"  [dim]Could not restore {name}: {e}[/dim]")

    console.print("\n[cyan]Clearing tc netem network faults...[/cyan]")
    for name in ["cartservice", "paymentservice", "currencyservice", "checkoutservice"]:
        _clear_tc_netem(name)
        console.print(f"  [green]tc cleared:[/green] {name}")

    console.print("\n[cyan]Reconnecting any partitioned networks...[/cyan]")
    for name in ["cartservice", "paymentservice"]:
        _docker_net_reconnect(client, name, NETWORK)

    console.print("\n[cyan]Resetting all feature flags to OFF...[/cyan]")
    ALL_FLAGS = [
        "paymentServiceFailure", "cartServiceFailure", "productCatalogFailure",
        "recommendationServiceCacheFailure", "adServiceFailure",
        "shippingServiceFailure", "loadgeneratorFloodHomepage",
    ]
    for flag in ALL_FLAGS:
        _flagd_set(flag, "off")

    console.print("\n[bold green]✔ All faults cleared. Stack restored to healthy baseline.[/bold green]\n")


# ──────────────────────────────────────────────
#  Dispatch
# ──────────────────────────────────────────────

def run_scenario(name: str) -> None:
    if name not in SCENARIOS:
        console.print(f"[bold red]Unknown scenario:[/bold red] '{name}'. Run --list to see all.")
        sys.exit(1)

    s = SCENARIOS[name]
    console.print(f"\n[bold yellow]┌─ Injecting Fault: [white]{name}[/white] ([cyan]{s['edge_case']}[/cyan])[/bold yellow]")
    console.print(f"[bold yellow]│  {s['description']}[/bold yellow]")
    console.print(f"[bold yellow]└─────────────────────────────────────────────[/bold yellow]\n")

    client = get_docker_client()
    method = s["method"]

    if method == "docker_mem_limit":
        _docker_mem_limit(client, s["target"], s["mem_limit"])
    elif method == "docker_cpu_limit":
        _docker_cpu_limit(client, s["target"], s["cpu_quota"], s["cpu_period"])
    elif method == "docker_stop":
        _docker_stop(client, s["target"])
    elif method == "docker_stop_multi":
        _docker_stop_multi(client, s["targets"])
    elif method == "tc_netem":
        _tc_netem(s["target"], s.get("delay_ms", 0), s.get("jitter_ms", 0), s.get("loss_pct", 0))
    elif method == "flagd":
        _flagd_set(s["flag"], s["value"])
    elif method == "redis_cmd":
        _redis_cmd(client, s["cmd"], s.get("args"))
    elif method == "docker_net_disconnect":
        _docker_net_disconnect(client, s["target"], s["network"])
    elif method == "restart_loop":
        _restart_loop(client, s["target"], s["cycles"], s["interval_s"])
    elif method == "clear_all":
        _clear_all(client)
        return

    console.print(f"\n[bold green]✔ Fault injected.[/bold green] Now run the pipeline to trigger the failure:")
    console.print(f"  [bold white]uv run python scripts/run_pipeline.py --scenario {name}[/bold white]")
    console.print(f"\n[dim]After capturing telemetry, restore with:[/dim]")
    console.print(f"  [bold white]uv run python scripts/inject_fault.py --scenario clear[/bold white]\n")


def list_scenarios() -> None:
    table = Table(
        title="ObservaSage — Fault Injection Scenarios",
        box=box.ROUNDED, show_header=True, header_style="bold magenta"
    )
    table.add_column("Scenario", style="bold white", no_wrap=True)
    table.add_column("EC", style="cyan", width=8)
    table.add_column("Method", style="dim", width=18)
    table.add_column("Description")

    ec_colors = {
        "EC-1": "yellow", "EC-2": "blue", "EC-3": "red",
        "EC-4": "magenta", "EC-5": "orange3", "UTIL": "green"
    }

    for name, s in SCENARIOS.items():
        ec = s["edge_case"]
        color = ec_colors.get(ec, "white")
        table.add_row(name, f"[{color}]{ec}[/{color}]", s["method"], s["description"])

    console.print(table)
    console.print(
        "\n[dim]EC-1 = OOM/Resource  EC-2 = Latency  EC-3 = Cascade  "
        "EC-4 = Silent Corruption  EC-5 = Infra Drift[/dim]\n"
    )


# ──────────────────────────────────────────────
#  Entry Point
# ──────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="ObservaSage Fault Injection Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--scenario", "-s", type=str, metavar="NAME", help="Scenario name to inject")
    group.add_argument("--list", "-l", action="store_true", help="List all available scenarios")
    args = parser.parse_args()

    if args.list:
        list_scenarios()
    else:
        run_scenario(args.scenario)


if __name__ == "__main__":
    main()
