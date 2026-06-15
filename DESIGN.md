# OPD Financial Outcome Agent — Process & Design Principles

## 1. Purpose

The OPD Financial Outcome Agent is a decision-support system for Andalusia Group's outpatient department leadership. It scans clinic performance data, diagnoses root causes, recommends actions, and narrates findings — replacing ad-hoc spreadsheet analysis with a reproducible, auditable pipeline.

---

## 2. System Architecture

The system is organized in four strict layers. Dependencies always point inward; outer layers never bypass inner ones.

```
┌──────────────────────────────────────────────────────┐
│  Presentation Layer  (app.py — Streamlit)            │
│  Tabs: Scanner · Action Plan · Executive Brief · Chat│
└───────────────────────┬──────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────┐
│  Application Layer  (src/application/)               │
│  Orchestrator · Scanner · Investigator               │
│  ActionRecommender · ExecutiveNarrator · KPIEngine   │
└───────────────────────┬──────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────┐
│  Infrastructure Layer  (src/infrastructure/)         │
│  DatasetRepository · KnowledgeRepository · GroqClient│
└───────────────────────┬──────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────┐
│  Domain Layer  (src/domain/models.py)                │
│  Flag · Investigation · ActionItem · ExecutiveBrief  │
│  KPIDefinition · KPIRelationship · PlaybookRule      │
└──────────────────────────────────────────────────────┘
```

---

## 3. Data Sources

| Source | File | Contents |
|---|---|---|
| OPD Fact Table | `data/OPD_dataset.xlsx` | Monthly KPI values per BU and Doctor |
| Knowledge Base | `data/Knowledge_base.xlsx` | 6 sheets (see below) |

### Knowledge Base Sheets

| Sheet prefix | Purpose |
|---|---|
| `adx_kpi_knowledge_map` | KPI definitions, owners, financial impact formulas, investigation steps |
| `adx_kpi_relationship_map` | Effect → Driver → Cause edges with weight and investigation order |
| `adx_kpi_formula_definition` | Formula logic per KPI |
| `adx_kpi_investigation_playbook` | Threshold rules, severity, recommended actions and escalation |
| `adx_kpi_filter_compatibility` | Which filters apply at which granularity |
| `adx_dim_kpi_scope` | Lowest available granularity and unavailability messages |

---

## 4. KPI Taxonomy

KPIs are organized in three layers. Investigation always starts at the Effect layer and drills down.

```
Effect KPIs          ← what leadership measures (revenue, cases)
    │
    ▼
Driver KPIs          ← operational levers (no-show, leakage, charge/case)
    │
    ▼
Cause KPIs           ← root issues (PMS compliance, slot utilization, CR%)
```

Each relationship carries:
- **Weight** — High / Medium / Low (investigation priority)
- **Investigation Order** — sequence the agent follows
- **Measurable** — whether the child KPI exists in the dataset (unmeasurable → manual check)

---

## 5. Pipeline Process

The system runs a four-stage pipeline, alternating deterministic and LLM steps.

```
┌─────────────────────────────────────────────────────────────────┐
│  Stage 1 — Performance Scanner  (deterministic)                 │
│  Applies 29 threshold rules across all BUs and Doctors          │
│  Output: ranked list of Flags (KPI, scenario, severity, EGP)    │
└──────────────────────────┬──────────────────────────────────────┘
                           │  top N BU-level flags
┌──────────────────────────▼──────────────────────────────────────┐
│  Stage 2 — Root-Cause Investigator  (LLM + tools)               │
│  Agent walks the KB relationship map (Effect → Driver → Cause)  │
│  Calls deterministic tools to verify each hypothesis            │
│  Output: Investigation (summary, confirmed causes, manual checks)│
└──────────────────────────┬──────────────────────────────────────┘
                           │  flags + investigations
┌──────────────────────────▼──────────────────────────────────────┐
│  Stage 3 — Action Recommender  (deterministic)                  │
│  Maps confirmed causes to KB recommended_action + action_owner  │
│  Output: ranked ActionItems (owner, escalation, EGP impact)     │
└──────────────────────────┬──────────────────────────────────────┘
                           │  all inputs
┌──────────────────────────▼──────────────────────────────────────┐
│  Stage 4 — Executive Narrator  (LLM)                            │
│  Composes a structured markdown brief (sections 1-3)            │
│  Section 4 (actions table) is injected deterministically        │
│  Output: ExecutiveBrief (markdown + raw data for download)      │
└─────────────────────────────────────────────────────────────────┘
```

### Stage 1 — Performance Scanner Rules

29 rules across 18 KPI IDs. Each rule defines: `actual`, `reference`, `variance`, `estimated_impact_egp`, and a human-readable `detail` string.

| # | KPI ID | KPI Name | Scenario | Severity | Calculated Criteria | Scope |
|---|---|---|---|---|---|---|
| 1 | OPD_KPI_003 | Total Revenue | Actual Revenue below Target | Critical | Actual Rev / Target Rev < 90% | BU + Doctor |
| 2 | OPD_KPI_003 | Total Revenue | Revenue declining MoM | High | Actual Rev < Prev Rev × (1 − 10%) | BU + Doctor |
| 3 | OPD_KPI_005 | Cash Revenue | Cash revenue declining | Medium | Cash Rev < Prev Cash Rev × (1 − 10%) | BU only |
| 4 | OPD_KPI_006 | Total Leakage Revenue Losses | Leakage losses increasing | Critical | Leakage / Total Rev > 10% | BU + Doctor |
| 5 | OPD_KPI_006 | Total Leakage Revenue Losses | Missed services increasing | High | Leakage > Prev Leakage × (1 + 15%) | BU only |
| 6 | OPD_KPI_007 | Doctor PMS % | Compliance metrics low | High | Doctor PMS % < 75% | Doctor only |
| 7 | OPD_KPI_007 | Doctor PMS % | PMS score below target | Medium | 75% ≤ Doctor PMS % < 80% | Doctor only |
| 8 | OPD_KPI_008 | No. Cases | Cases below target | High | No. Cases / Target Cases < 85% | BU + Doctor |
| 9 | OPD_KPI_008 | No. Cases | Sudden drop in patient volume | Critical | No. Cases < Prev Cases × (1 − 15%) | BU only |
| 10 | OPD_KPI_010 | Charge per case | Avg revenue per case low | High | Charge/Case < 12m Trailing Avg × (1 − 10%) | BU + Doctor |
| 11 | OPD_KPI_011 | No. Booking | Digital leads high but bookings low | Critical | Digital Actual CR% < Digital Target CR% | BU only |
| 12 | OPD_KPI_011 | No. Booking | Booking below expected | High | No. Booking / Planned Slots < 85% | BU only |
| 13 | OPD_KPI_012 | No. Planned booking Slots | Slot utilization low | Medium | No. Cases / Planned Slots < 70% | BU only |
| 14 | OPD_KPI_012 | No. Planned booking Slots | Insufficient capacity | High | No. Booking > Planned Slots | BU only |
| 15 | OPD_KPI_013 | No. follow-up visits | Follow-up visits declining | Medium | Follow-up < Prev Follow-up × (1 − 15%) | BU only |
| 16 | OPD_KPI_013 | No. follow-up visits | Low doctor follow-up engagement | High | Follow-up / No. Cases < 70% | BU only |
| 17 | OPD_KPI_014 | Service Leakage % | Leakage percentage high | Critical | Service Leakage % > 8% | BU only |
| 18 | OPD_KPI_015 | Cross Referral % | Cross referral rate low | Medium | Cross Referral % < 10% AND Digital Actual CR% < 10% | BU only |
| 19 | OPD_KPI_015 | Cross Referral % | Referral decline in COE pathways | High | Cross Referral % < Prev × (1 − 20%) | BU only |
| 20 | OPD_KPI_016 | Patient Retention % | Retention below target | High | Patient Retention % < 60% | BU only |
| 21 | OPD_KPI_016 | Patient Retention % | No-show affecting retention | Medium | No-Show % > 25% | BU only |
| 22 | OPD_KPI_018 | Actual COE Compliance % | COE compliance low | Critical | COE Compliance % < 80% | BU only |
| 23 | OPD_KPI_018 | Actual COE Compliance % | Missed COE referrals increasing | High | COE % < Prev COE % × (1 − 15%) | BU only |
| 24 | OPD_KPI_019 | Digital Actual CR% | Actual CR below target | High | Actual CR% < Target CR% − 10pp | BU only |
| 25 | OPD_KPI_021 | No. Missed Opportunity | Missed opportunities increasing | High | Missed Opp > Prev Missed Opp × (1 + 10%) | BU only |
| 26 | OPD_KPI_022 | No. Cancelled Clinics | Clinic cancellations increasing | High | Cancelled Clinics / Planned Slots > 5% | BU only |
| 27 | OPD_KPI_022 | No. Cancelled Clinics | High revenue impact from cancellations | Critical | Cancellation Losses / Total Rev > 8% | BU only |
| 28 | OPD_KPI_023 | Total Losses Rev Cancellation/Modification | Revenue losses increasing | Critical | Cancellation Losses / Total Rev > 8% | BU + Doctor |
| 29 | OPD_KPI_024 | No-Show % | No-show rate high | Critical | No-Show % > 25% | BU + Doctor |

### Stage 2 — Agent Tools

The investigator and chat agents share the same 5 deterministic tools. The LLM never computes numbers; it only calls tools and interprets results.

| Tool | Purpose |
|---|---|
| `kpi_snapshot` | All KPI values for one period and scope vs prior month |
| `kpi_trend` | Monthly time series for one KPI (mean / min / max over n months) |
| `rank_contributors` | Rank BUs or Doctors by variance on a KPI (worst first) |
| `quantify_impact` | EGP breakdown of all loss buckets for a scope |
| `kb_lookup` | Full KB entry for a KPI: definition, drivers, playbook, owner, escalation |

---

## 6. Domain Objects

| Object | Created by | Purpose |
|---|---|---|
| `Flag` | Scanner | A threshold breach: KPI, scenario, severity, actual vs reference, EGP impact, detail |
| `Investigation` | Investigator | Confirmed causes, ruled-out causes, manual checks, tool-call trace |
| `CauseFinding` | Investigator | One hypothesis: verdict, evidence text, optional EGP impact |
| `ActionItem` | ActionRecommender | Owner-mapped action from KB with severity and EGP |
| `ExecutiveBrief` | Narrator | Final markdown report + raw flags/investigations/actions |
| `KPIDefinition` | KB | Full KPI metadata including formula, investigation steps, owner |
| `KPIRelationship` | KB | Parent → child edge with weight, order, and measurability flag |
| `PlaybookRule` | KB | Threshold scenario with severity, action and escalation |

---

## 7. Configuration

All tunables are in `src/config.py` — no magic numbers anywhere else.

| Section | Key settings |
|---|---|
| `LLMConfig` | Model names (investigator / narrator / router), `max_tokens`, `max_tool_iterations` |
| `ScannerThresholds` | All 20+ threshold values aligned with the KB playbook |
| `AppConfig` | File paths for dataset and knowledge base |
| `KPI_NAME_ALIASES` | Mapping between KB KPI names and dataset column names |

---

## 8. Design Principles

### P1 — Determinism is the default; LLM is the exception
Every number in the system comes from `KPIEngine` (pure pandas). The LLM never computes, estimates, or invents figures. It only calls tools and interprets their results. This makes every output reproducible and auditable.

### P2 — The knowledge base is the single source of truth
Thresholds, action owners, escalation levels, recommended actions, and investigation sequences are read from the KB at runtime — never hardcoded into the LLM prompt. Updating the KB updates the system's behavior without touching code.

### P3 — Layers have no upward dependencies
The domain layer imports nothing from other layers. The application layer imports only domain and infrastructure. The presentation layer (Streamlit) is a pure rendering skin. This makes each layer independently testable.

### P4 — Flags are ranked before investigation
The scanner produces a full ranked list (Critical → High → Medium → Low, then by descending EGP impact) before any LLM call. Investigations are allocated from the top of this list. If the LLM budget is exhausted, the most impactful issues are always covered first.

### P5 — BU-level before doctor-level
The investigator always targets BU-level flags first. Doctor-level attribution is surfaced by calling `rank_contributors(dimension="Doctor")` from within the BU investigation — never by running a separate doctor-level investigation upfront. This prevents noise from individual doctor variance dominating the agenda.

### P6 — Unmeasurable causes are named, never guessed
When the KB relationship map marks a child KPI as `measurable_in_dataset=false`, the agent records it as a `manual_check` with the responsible owner. It never queries data that does not exist or infers the value from proxies.

### P7 — Separation of financial quantification from narrative
`estimated_impact_egp` is computed by the scanner using KB `Financial_Impact_Formula` logic — not by the LLM narrator. The narrator writes prose; the scanner writes numbers. This prevents hallucinated EGP figures in executive output.

### P8 — Section 4 of the executive brief is always deterministic
The action table in the executive brief is injected by the application layer (replacing whatever the LLM generated for section 4) sourced directly from the same `session_state["flags"]` that drives the Action Plan tab. Leadership always sees the same actions in both places.

### P9 — Graceful degradation on LLM failure
If the Groq API is unavailable or returns an error, the scanner and action recommender continue to function. The executive brief tab simply shows an error; the scanner and action plan tabs are unaffected. Tool-call parse failures are recovered via a two-pass regex fallback before raising.

### P10 — Ratio KPIs aggregate as mean, amount KPIs aggregate as sum
`DatasetRepository.aggregate()` applies `mean()` to percentage/ratio columns (defined in `RATIO_COLUMNS`) and `sum()` to volume/revenue columns. `Charge per case` is always recomputed as `Total Revenue ÷ No. Cases` after aggregation — never averaged — to preserve ratio semantics across doctor-to-BU rollups.

---

## 9. Conversation Agent

The chat agent (`Orchestrator.ask`) uses the same 5 tools as the investigator and responds in a structured 5-part format:

1. **Direct answer** — key metric with on/off-track verdict
2. **What this means** — plain-language translation of the numbers
3. **Root cause chain** — Effect → Driver → Cause logic with tool evidence
4. **Financial impact** — EGP quantification with stated assumptions
5. **Recommended next steps** — 2-3 actions with named owner

Conversation history is maintained client-side in `st.session_state["chat"]` and the last 6 turns are prepended to each request for context continuity.

---

## 10. File Map

```
opd_financial_agent_Groq/
├── app.py                          # Streamlit UI (presentation only)
├── src/
│   ├── config.py                   # All tunables and thresholds
│   ├── domain/
│   │   └── models.py               # Pure domain entities
│   ├── application/
│   │   ├── orchestrator.py         # Composition root + chat agent
│   │   ├── scanner.py              # Stage 1 — 29 deterministic rules
│   │   ├── investigator.py         # Stage 2 — LLM root-cause agent
│   │   ├── actions_narrator.py     # Stage 3 recommender + Stage 4 narrator
│   │   ├── kpi_engine.py           # Deterministic tool implementations
│   │   └── tools.py                # Tool schemas + dispatcher
│   └── infrastructure/
│       ├── repositories.py         # Excel loading (dataset + KB)
│       └── llm_client.py           # Groq API wrapper with fallback recovery
└── data/
    ├── OPD_dataset.xlsx            # OPD fact table
    └── Knowledge_base.xlsx         # 6-sheet knowledge base
```
