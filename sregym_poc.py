"""PoC: run the opensre agent against a live SREGym problem."""

from __future__ import annotations

import os
from typing import Any

SREGYM_URL = os.getenv("SREGYM_URL", "http://127.0.0.1:9954")
NAMESPACE = os.getenv("SREGYM_NAMESPACE", "hotel-reservation")


def sregym_integrations() -> dict[str, Any]:
    return {
        "sregym": {
            "connection_verified": True,
            "url": SREGYM_URL,
            "endpoints": ["kubectl", "submit"],
            "auth_token": "",
        }
    }


def build_alert() -> dict[str, Any]:
    return {
        "alert_name": "SREGym benchmark incident",
        "alert_source": "sregym",
        "severity": "critical",
        "kube_namespace": NAMESPACE,
        "message": (
            f"Services in Kubernetes namespace {NAMESPACE} are failing. "
            "Investigate the live cluster, determine the root cause, and apply a fix."
        ),
    }


def main() -> None:
    os.environ.setdefault("SREGYM_URL", SREGYM_URL)
    os.environ.setdefault("SREGYM_ENDPOINTS", "kubectl submit")

    from tools.investigation.capability import build_investigation_payload, run_investigation

    state = run_investigation(build_alert(), resolved_integrations=sregym_integrations())
    payload = build_investigation_payload(state)

    print("=" * 70)
    print("ROOT CAUSE:")
    print(payload.get("root_cause"))
    print("-" * 70)
    print("VALIDITY:", payload.get("validity_score"))
    calls = payload.get("tool_calls") or []
    print("TOOL CALLS:", len(calls))
    for call in calls[:20]:
        print("  -", call)


if __name__ == "__main__":
    main()
