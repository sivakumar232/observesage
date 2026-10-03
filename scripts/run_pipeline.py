#!/usr/bin/env python3
"""
CI/CD Pipeline Simulator for ObservaSage.
Executes an automated integration test against the microservices:
1. Loads home / product catalog
2. Adds items to cart
3. Executes checkout
4. Records execution window [t_start, t_end] and saves run metadata to data/runs/
"""

import sys
import os
import json
import time
import uuid
import datetime
import argparse
import requests
from rich.console import Console

console = Console()

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:8080")
RUNS_DIR = os.getenv("DATA_RUNS_DIR", "data/runs")

def run_integration_pipeline(user_id: str):
    """Simulates an end-to-end user checkout journey."""
    session = requests.Session()
    steps_passed = []

    # Step 1: Healthcheck & home page
    t0 = time.time()
    resp = session.get(f"{FRONTEND_URL}/", timeout=5.0)
    if resp.status_code not in [200, 301, 302]:
        raise RuntimeError(f"Step 1 Failed: GET / returned {resp.status_code}")
    steps_passed.append({"step": "browse_home", "duration_ms": round((time.time() - t0) * 1000, 2)})

    # Step 2: Add item to cart
    t0 = time.time()
    add_payload = {
        "userId": user_id,
        "item": {
            "productId": "OLJCESPC7Z",
            "quantity": 1
        }
    }
    resp = session.post(f"{FRONTEND_URL}/api/cart", json=add_payload, timeout=5.0)
    # Even if demo frontend returns 200 or 404 (due to subset), record step
    if resp.status_code >= 500:
        raise RuntimeError(f"Step 2 Failed: POST /api/cart returned server error {resp.status_code}")
    steps_passed.append({"step": "add_to_cart", "status_code": resp.status_code, "duration_ms": round((time.time() - t0) * 1000, 2)})

    # Step 3: Checkout
    t0 = time.time()
    checkout_payload = {
        "userId": user_id,
        "userCurrency": "USD",
        "email": f"{user_id}@example.com",
        "address": {
            "streetAddress": "1600 Amphitheatre Pkwy",
            "city": "Mountain View",
            "state": "CA",
            "country": "USA",
            "zipCode": "94043"
        },
        "creditCard": {
            "creditCardNumber": "4111111111111111",
            "creditCardCvv": 123,
            "creditCardExpirationYear": 2028,
            "creditCardExpirationMonth": 12
        }
    }
    resp = session.post(f"{FRONTEND_URL}/api/checkout", json=checkout_payload, timeout=8.0)
    if resp.status_code >= 500:
        raise RuntimeError(f"Step 3 Failed: POST /api/checkout returned server error {resp.status_code}")
    steps_passed.append({"step": "checkout", "status_code": resp.status_code, "duration_ms": round((time.time() - t0) * 1000, 2)})

    return steps_passed

def main():
    parser = argparse.ArgumentParser(description="ObservaSage CI/CD Pipeline Simulator")
    parser.add_argument("--run-id", type=str, default=None, help="Custom Run ID")
    parser.add_argument("--scenario", type=str, default="normal", help="Label for this run scenario")
    args = parser.parse_args()

    run_id = args.run_id or f"run_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    user_id = f"test_user_{uuid.uuid4().hex[:8]}"

    console.print(f"[bold cyan]▶ Starting Pipeline Run: [white]{run_id}[/white] (Scenario: [yellow]{args.scenario}[/yellow])[/bold cyan]")
    
    t_start = datetime.datetime.now(datetime.timezone.utc)
    status = "SUCCESS"
    error_message = None
    steps = []

    try:
        steps = run_integration_pipeline(user_id)
        console.print("[bold green]✔ Pipeline Run Completed Successfully![/bold green]")
    except Exception as e:
        status = "FAILED"
        error_message = str(e)
        console.print(f"[bold red]✖ Pipeline Run Failed:[/bold red] {e}")

    t_end = datetime.datetime.now(datetime.timezone.utc)
    duration_s = (t_end - t_start).total_seconds()

    run_meta = {
        "run_id": run_id,
        "scenario": args.scenario,
        "status": status,
        "start_time": t_start.isoformat(),
        "end_time": t_end.isoformat(),
        "duration_seconds": round(duration_s, 3),
        "error_message": error_message,
        "steps": steps
    }

    subfolder = "success" if status == "SUCCESS" else "failed"
    target_dir = os.path.join(RUNS_DIR, subfolder)
    os.makedirs(target_dir, exist_ok=True)
    meta_path = os.path.join(target_dir, f"{run_id}_meta.json")

    with open(meta_path, "w") as f:
        json.dump(run_meta, f, indent=2)

    console.print(f"[dim]Run metadata saved to: {meta_path}[/dim]\n")
    sys.exit(0 if status == "SUCCESS" else 1)

if __name__ == "__main__":
    main()
