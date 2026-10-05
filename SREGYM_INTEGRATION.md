# SREGym Integration for OpenSRE (MCP-based)

This branch integrates the **OpenSRE** agent with the **[SREGym](https://github.com/SREGym/SREGym)**
benchmark so that OpenSRE can be evaluated on live Kubernetes fault scenarios,
investigating and **acting on** a broken cluster, not just describing what it would do.

It was built as part of the UIUC++ SRSE 2026 research program (Prof. Tianyin Xu's group),
as an alternative, MCP-based approach to connecting OpenSRE to SREGym.

---

## Approach

SREGym exposes its agent interface as **MCP tool-servers over SSE** — five endpoints
(`/kubectl`, `/jaeger`, `/loki`, `/prometheus`, `/submit`). Rather than running OpenSRE
as an external subprocess, this integration connects OpenSRE **directly** to those
endpoints and drives investigations through OpenSRE's own programmatic
`run_investigation` API. SREGym injects a fault, and OpenSRE investigates the live
cluster from a minimal generated alert, no per-problem alert files are required.

Because OpenSRE's `/kubectl` tool (`exec_kubectl_cmd_safely`) executes real kubectl
commands (read **and** write), this path lets OpenSRE perform both **diagnosis** and
**mitigation** itself, without handing the repair to a separate agent.

---

## What was added

### 1. A `sregym` integration (`integrations/sregym/`)
Modeled on OpenSRE's existing MCP integrations:
- `__init__.py` — connection/config over SSE, plus `list_sregym_tools` / `call_sregym_tool`
- `verifier.py` — connection verifier (auto-discovered)
- `tools/sregym_tool/__init__.py` — the two agent-callable tools (`list_sregym_tools`, `call_sregym_tool`)

### 2. Wiring across four core files
Adding a new environment to OpenSRE is not purely additive; it required:
- `core/domain/types/evidence.py` — add `"sregym"` to the evidence-source literal
- `integrations/registry.py` — register the `sregym` integration spec
- `tools/registry_discovery.py` — add the integration's tool package to discovery
- `core/domain/alerts/alert_source.py` — add alert-source routing (without this the
  planner scored the SREGym tools out entirely and the agent never called them)
- `tools/investigation/stages/intake/node.py` — add `sregym` to alert classification

### 3. A mitigation agent (`MitigationInvestigationAgent`)
Added in `tools/investigation/stages/gather_evidence/agent.py`.

OpenSRE's investigation loop normally concludes as soon as it can *explain* a fault,
so in mitigation mode it would **recommend** a fix but stop short of **executing** it.
This subclass overrides the loop's conclusion policy: it refuses to conclude until a
**state-changing kubectl command has actually run**, nudging the agent to act before
writing its final answer.

### 4. Runners
- `sregym_poc.py` — diagnosis only
- `sregym_mitigation_poc.py` — diagnosis, then mitigation using `MitigationInvestigationAgent`

---

## How to run

Prerequisites: a running SREGym cluster with its MCP server up (default
`http://127.0.0.1:9954`), a deployed+injected problem, and an LLM configured in
OpenSRE's `.env`.

```bash
# diagnosis only
uv run python sregym_poc.py

# diagnosis + autonomous mitigation
uv run python sregym_mitigation_poc.py
```

The integration is enabled by injecting a `resolved_integrations` dict (see the runners),
so credential discovery is skipped and no persistent OpenSRE config is modified.

---

## Status (honest)

- **Integration:** working — OpenSRE connects to SREGym over SSE, discovers the tools,
  and executes real kubectl commands against the live cluster.
- **Diagnosis:** working — on a live cluster the agent autonomously issues kubectl
  commands and produces an evidence-grounded root cause. (Notably, with only generic
  knowledge tools it would confidently hallucinate a root cause it had never verified;
  with the real tools it produces grounded answers.)
- **Mitigation execution:** working — the `MitigationInvestigationAgent` forces the agent
  to execute its own fixes. Demonstrated on two SREGym problems:
  - `missing_configmap_hotel_reservation` — the agent ran `kubectl create configmap`
    + `rollout restart`, and the affected pod recovered.
  - `wrong_service_selector_hotel_reservation` — the agent executed a `kubectl patch service`
    command (the execution mechanism fired), though the exact patch did not fully land
    (a selector-key-removal patch the model got slightly wrong — a model-quality limitation,
    not an integration one).
- **Not yet done:** running through SREGym's mitigation **oracle** for an official graded
  score (fixes here were verified manually via kubectl), and merging this MCP-direct
  approach with the parallel subprocess-based integration (blocked by an environment-level
  package-name collision between the two projects: OpenSRE's `platform` package vs.
  Python's built-in `platform`).

---

## Notes

- This is research/prototype work on a fork; it is not intended for upstream merge into
  OpenSRE, since the `sregym` integration is specific to the SREGym benchmark.
- The diagnosis-side integration was developed in parallel by a teammate using a
  subprocess approach; this branch is the MCP-direct alternative, with its main added
  value being that OpenSRE performs the mitigation itself.
