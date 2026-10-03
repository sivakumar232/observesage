#!/usr/bin/env python3
"""
Stack Health Checker for ObservaSage.
Verifies connectivity and readiness of Docker containers, Prometheus, and Jaeger.
"""

import sys
import time
import requests
import docker
from rich.console import Console
from rich.table import Table

console = Console()

SERVICES_TO_CHECK = [
    ("Prometheus API", "http://localhost:9090/-/ready", 200),
    ("Jaeger Tracing UI", "http://localhost:16686", 200),
    ("Frontend UI / API", "http://localhost:8080", [200, 301, 302, 404]),
]

EXPECTED_CONTAINERS = [
    "prometheus",
    "jaeger",
    "otel-collector",
    "flagd",
    "redis-cart",
    "cartservice",
    "productcatalogservice",
    "quoteservice",
    "shippingservice",
    "emailservice",
    "currencyservice",
    "paymentservice",
    "checkoutservice",
    "frontend",
]

def check_docker_containers():
    console.print("\n[bold cyan]1. Checking Docker Containers...[/bold cyan]")
    table = Table(title="Container Status", show_header=True, header_style="bold magenta")
    table.add_column("Container", style="dim")
    table.add_column("Status")
    table.add_column("State")

    all_running = True
    try:
        client = docker.from_env()
        running_names = {c.name: c for c in client.containers.list(all=True)}
        
        for name in EXPECTED_CONTAINERS:
            if name in running_names:
                c = running_names[name]
                status_color = "green" if c.status == "running" else "red"
                table.add_row(name, f"[{status_color}]{c.status}[/{status_color}]", c.attrs['State']['Status'])
                if c.status != "running":
                    all_running = False
            else:
                table.add_row(name, "[red]NOT FOUND[/red]", "missing")
                all_running = False
    except Exception as e:
        console.print(f"[bold red]Docker Engine Error:[/bold red] {e}")
        return False

    console.print(table)
    return all_running

def check_http_endpoints():
    console.print("\n[bold cyan]2. Checking Observability & Service Endpoints...[/bold cyan]")
    table = Table(title="HTTP Endpoints Readiness", show_header=True, header_style="bold magenta")
    table.add_column("Service")
    table.add_column("URL")
    table.add_column("Result")

    all_ok = True
    for name, url, expected in SERVICES_TO_CHECK:
        try:
            resp = requests.get(url, timeout=3.0)
            expected_codes = [expected] if isinstance(expected, int) else expected
            if resp.status_code in expected_codes:
                table.add_row(name, url, f"[green]HEALTHY ({resp.status_code})[/green]")
            else:
                table.add_row(name, url, f"[yellow]UNEXPECTED STATUS ({resp.status_code})[/yellow]")
                all_ok = False
        except requests.exceptions.RequestException as e:
            table.add_row(name, url, f"[red]UNREACHABLE ({type(e).__name__})[/red]")
            all_ok = False

    console.print(table)
    return all_ok

def main():
    console.print("[bold yellow]=====================================================[/bold yellow]")
    console.print("[bold yellow]       ObservaSage Stack Healthcheck                  [/bold yellow]")
    console.print("[bold yellow]=====================================================[/bold yellow]")

    docker_ok = check_docker_containers()
    endpoints_ok = check_http_endpoints()

    if docker_ok and endpoints_ok:
        console.print("\n[bold green]✔ All ObservaSage services and observability backends are healthy![/bold green]\n")
        sys.exit(0)
    else:
        console.print("\n[bold red]✖ Some services are unhealthy or offline. Start with: docker compose up -d[/bold red]\n")
        sys.exit(1)

if __name__ == "__main__":
    main()
