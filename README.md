# OPD Financial Outcome Agent

An agentic analytics application for Andalusia Group's OPD leadership. It scans
clinic performance against targets, investigates root causes by walking the
organisation's official KPI knowledge base (Effect → Driver → Cause), maps
confirmed causes to governed actions/owners/escalation levels, and writes an
executive brief. Includes a conversational "Ask the agent" mode over the same
tools.

## Design principle

**The LLM never does arithmetic.** All variance, trend, ranking and impact math
runs in deterministic pandas (`KPIEngine`). Claude's roles are: deciding which
hypothesis to test next (Investigator), and writing the narrative (Narrator).
Actions, owners and escalation levels are joined verbatim from the knowledge
base — never generated. Drivers referenced by the KB but not measurable in the
dataset (e.g. Approval Rate, Walk-in Volume, Service Mix) are routed to
*manual verification* with their owner, never guessed.

## Architecture (clean architecture layout)

```
opd_financial_agent/
├── app.py                          # PRESENTATION  — Streamlit UI only
├── data/                           # OPD_dataset.xlsx, Knowledge_base.xlsx
└── src/
    ├── config.py                   # tunables: models, thresholds, KPI aliases
    ├── domain/
    │   └── models.py               # ENTITIES — pure dataclasses, zero dependencies
    ├── application/                # USE CASES — business logic
    │   ├── kpi_engine.py           #   deterministic tool layer (all math)
    │   ├── scanner.py              #   Stage 1: deterministic variance flagging
    │   ├── tools.py                #   Anthropic tool schemas + dispatcher
    │   ├── investigator.py         #   Stage 2: LLM root-cause agent
    │   ├── actions_narrator.py     #   Stage 3 (deterministic) + Stage 4 (LLM)
    │   └── orchestrator.py         #   composition root + ad-hoc Q&A agent
    └── infrastructure/             # ADAPTERS — external world
        ├── repositories.py         #   Excel loading, KB sheets, alias resolution
        └── llm_client.py           #   Anthropic SDK wrapper + tool-use loop
```

Dependency direction: `presentation → application → domain`, with
`infrastructure` injected at the composition root (`Orchestrator`). Domain
imports nothing; swapping Excel for SQL Server, or Anthropic for another
provider, touches only `infrastructure/`.

## Pipeline

1. **Performance scanner** (deterministic) — playbook-aligned rules per BU and
   doctor: revenue <90% of target (Critical), cases <85%, MoM revenue drop
   >10%, leakage/cancellation losses vs revenue, no-show and charge-per-case
   vs trailing 12-month baselines, digital CR and COE compliance. Flags are
   ranked by severity then estimated EGP impact.
2. **Root-cause investigator** (Claude + tools) — for each top flag, walks the
   `adx_kpi_relationship_map` in `Investigation_Order`, calling
   `kpi_snapshot` / `kpi_trend` / `rank_contributors` / `quantify_impact` /
   `kb_lookup` to confirm or rule out each driver. Full tool-call audit trail
   is kept and shown in the UI.
3. **Action recommender** (deterministic) — joins confirmed causes to
   `Recommended_Action`, `Action_Owner`, `Escalation_Level`.
4. **Executive narrator** (Claude) — composes the leadership brief from the
   structured results; a template fallback runs when no API key is set.

## Models (Groq free tier — no credit card)

| Role | Default | Why |
|---|---|---|
| Investigator | `llama-3.3-70b-versatile` | best quality + tool use on Groq free tier |
| Narrator | `llama-3.3-70b-versatile` | executive-quality writing |
| Router (reserved) | `llama-3.1-8b-instant` | cheap classification if you add triage |

Free tier limits: 30 RPM, 14 400 RPD, 6 000 TPM. The client backs off
automatically on rate-limit (429) responses. Sign up at console.groq.com.

Change in `src/config.py` or via env if newer models are available.

## Run

```bash
pip install -r requirements.txt
export GROQ_API_KEY=gsk_...   # or paste it in the sidebar
streamlit run app.py
```

Without an API key the scanner, drill-downs, action plan and template brief
still work — useful for validating the deterministic layer with stakeholders
before spending tokens.

## Extending

- **SQL Server / DWH source**: implement a `DatasetRepository` variant reading
  from your warehouse; nothing else changes.
- **Scheduling**: call `Orchestrator.run_full_pipeline(...)` from a cron /
  Power Automate-triggered script and email/post the markdown brief.
- **Closing the loop**: persist `ActionItem`s and have next month's run report
  whether each flagged KPI recovered.
