#!/usr/bin/env python3
"""
Deterministic Fault Injection Engine for ObservaSage.
Injects real faults at the container and network layer:
- oom: Restricts memory limit on container to force exit code 137
- latency: Introduces artificial delay in network response
- crash: Stops / kills a service to trigger cascading errors
- clear: Restores all containers to healthy defaults
"""

import sys
import argparse
import docker
from rich.console import Console

console = Console()

def get_docker_client():
    try:
        return docker.from_env()
    except Exception as e:
        console.print(f"[bold red]Failed to connect to Docker daemon:[/bold red] {e}")
        sys.exit(1)

def inject_oom(client, service_name: str, memory_limit: str = "30m"):
    console.print(f"[bold yellow]Injecting OOM fault on [{service_name}] (Memory limit: {memory_limit})...[/bold yellow]")
    try:
        container = client.containers.get(service_name)
        # Update memory limit to trigger Linux OOM-killer
        container.update(mem_limit=memory_limit, memswap_limit=memory_limit)
        console.print(f"[bold green]✔ Restricted memory on {service_name} to {memory_limit}. Run pipeline to trigger OOM kill![/bold green]")
    except Exception as e:
        console.print(f"[bold red]Error updating container memory:[/bold red] {e}")

def inject_crash(client, service_name: str):
    console.print(f"[bold yellow]Injecting Service Crash fault by pausing/stopping [{service_name}]...[/bold yellow]")
    try:
        container = client.containers.get(service_name)
        container.stop(timeout=1)
        console.print(f"[bold green]✔ Service {service_name} stopped. Downstream calls will fail with cascade error![/bold green]")
    except Exception as e:
        console.print(f"[bold red]Error stopping container:[/bold red] {e}")

def clear_faults(client):
    console.print("[bold cyan]Restoring all containers to healthy baseline defaults...[/bold cyan]")
    containers_to_restore = [
        "cartservice", "paymentservice", "currencyservice", 
        "checkoutservice", "frontend", "redis-cart"
    ]
    for name in containers_to_restore:
        try:
            c = client.containers.get(name)
            # Remove memory limits
            c.update(mem_limit=0, memswap_limit=0)
            if c.status != "running":
                console.print(f"Restarting stopped container: {name}...")
                c.start()
            console.print(f"[green]✔ Restored {name} to healthy state[/green]")
        except Exception as e:
            console.print(f"[dim]Could not restore {name}: {e}[/dim]")

def main():
    parser = argparse.ArgumentParser(description="ObservaSage Fault Injection CLI")
    parser.add_argument("--fault", type=str, choices=["oom", "crash", "clear"], required=True, help="Type of fault to inject")
    parser.add_argument("--service", type=str, default="cartservice", help="Target service name")
    parser.add_argument("--memory-limit", type=str, default="25m", help="Memory limit string for OOM (e.g. 25m)")
    args = parser.parse_args()

    client = get_docker_client()

    if args.fault == "oom":
        inject_oom(client, args.service, args.memory_limit)
    elif args.fault == "crash":
        inject_crash(client, args.service)
    elif args.fault == "clear":
        clear_faults(client)

if __name__ == "__main__":
    main()
