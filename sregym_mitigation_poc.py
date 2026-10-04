"""PoC: opensre diagnoses a SREGym problem, then applies its own fix."""

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


def diagnosis_alert() -> dict[str, Any]:
    return {
        "alert_name": "SREGym benchmark incident",
        "alert_source": "sregym",
        "severity": "critical",
        "kube_namespace": NAMESPACE,
        "message": (
            f"A fault was injected into the app in namespace {NAMESPACE}. "
            "Investigate the live cluster and determine the root cause."
        ),
    }


def mitigation_alert(root_cause: str) -> dict[str, Any]:
    return {
        "alert_name": "SREGym mitigation",
        "alert_source": "sregym",
        "severity": "critical",
        "kube_namespace": NAMESPACE,
        "message": (
            f"The root cause is ALREADY KNOWN - do not re-diagnose:\n\n{root_cause}\n\n"
            "Your only job is to FIX the live cluster. Use exec_kubectl_cmd_safely "
            "to run a state-changing kubectl command (apply/create/expose/patch/"
            "rollout restart/scale/delete) that resolves this, then verify with a "
            "read command. Act autonomously; do not just recommend."
        ),
    }


def main() -> None:
    os.environ.setdefault("SREGYM_URL", SREGYM_URL)
    os.environ.setdefault("SREGYM_ENDPOINTS", "kubectl submit")

    from tools.investigation.capability import build_investigation_payload, run_investigation
    from tools.investigation.stages.gather_evidence.agent import MitigationInvestigationAgent

    print("=" * 70, "\nDIAGNOSIS")
    dstate = run_investigation(diagnosis_alert(), resolved_integrations=sregym_integrations())
    dpayload = build_investigation_payload(dstate)
    root_cause = dpayload.get("root_cause") or ""
    print("ROOT CAUSE:", root_cause)
    print("diagnosis tool calls:", len(dpayload.get("tool_calls") or []))

    print("=" * 70, "\nMITIGATION")
    mstate = run_investigation(
        mitigation_alert(root_cause),
        resolved_integrations=sregym_integrations(),
        agent_class=MitigationInvestigationAgent,
    )
    mpayload = build_investigation_payload(mstate)
    calls = mpayload.get("tool_calls") or []
    print("mitigation tool calls:", len(calls))
    for c in calls:
        args = c.get("tool_args") if isinstance(c, dict) else None
        cmd = ""
        if isinstance(args, dict):
            cmd = args.get("cmd") or (args.get("arguments") or {}).get("cmd") or ""
        if cmd:
            print("  CMD:", cmd)
    print("-" * 70)
    print("mitigation report:", mpayload.get("root_cause"))


if __name__ == "__main__":
    main()
