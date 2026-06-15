"""Orchestrator — composition root for the pipeline, plus the ad-hoc Q&A agent.

Pipeline:  scan (deterministic) -> investigate top flags (LLM)
           -> recommend actions (deterministic) -> narrate (LLM).
"""
from __future__ import annotations

import calendar
import json

from src.application.actions_narrator import ActionRecommender, ExecutiveNarrator
from src.application.investigator import RootCauseInvestigator
from src.application.kpi_engine import KPIEngine
from src.application.scanner import PerformanceScanner
from src.application.tools import TOOL_SCHEMAS, make_dispatcher
from src.config import AppConfig
from src.domain.models import ExecutiveBrief, Flag, Investigation
from src.infrastructure.llm_client import GroqClient
from src.infrastructure.repositories import DatasetRepository, KnowledgeRepository

CHAT_SYSTEM = """You are the OPD Financial Outcome Agent for Andalusia Group's \
leadership. Answer questions about OPD clinic performance, revenue drivers and \
areas of improvement using ONLY numbers obtained from your tools — never invent \
or compute figures yourself. Use kb_lookup to follow the organisation's official \
Effect -> Driver -> Cause investigation logic. If a driver is not measurable in \
the dataset, say so and name the owner who should verify it manually. \
Currency is EGP.

Structure every answer as follows:
1. **Direct answer** — state the key metric(s) and whether they are on track or not.
2. **What this means** — translate the numbers into plain business language \
(e.g. "this means 120 patients did not show up, costing roughly EGP 240,000").
3. **Root cause chain** — walk through the Effect → Driver → Cause logic using \
actual tool results. Explain *why* each driver matters and how it connects to the \
headline KPI.
4. **Financial impact** — quantify EGP exposure where the data allows; state \
assumptions clearly.
5. **Recommended next steps** — 2-3 concrete actions with the responsible owner.

Use markdown headers or bullet points to keep the answer scannable. \
Be thorough but focused — every sentence should add information the reader can act on."""


class Orchestrator:
    def __init__(self, config: AppConfig, api_key: str | None = None):
        self.config = config
        self.data = DatasetRepository(config.dataset_path)
        self.kb = KnowledgeRepository(config.knowledge_base_path, self.data.kpi_columns)
        self.engine = KPIEngine(self.data, self.kb)
        self.llm = GroqClient(config.llm, api_key=api_key)
        self.scanner = PerformanceScanner(self.engine, config.thresholds)
        self.investigator = RootCauseInvestigator(self.engine, self.llm)
        self.recommender = ActionRecommender(self.kb)
        self.narrator = ExecutiveNarrator(self.llm)

    # ------------------------------------------------------------- pipeline
    def scan(self, year: int, month: int, include_doctor_level: bool = True) -> list[Flag]:
        return self.scanner.scan(year, month, include_doctor_level=include_doctor_level)

    def investigate_flags(self, flags: list[Flag], max_investigations: int = 3,
                          on_event=None) -> dict[str, Investigation]:
        """Investigate the top-N flags (BU-level first — doctor-level detail is
        usually surfaced by the agent itself via rank_contributors)."""
        bu_level = [f for f in flags if not f.scope.get("doctor")]
        targets = (bu_level or flags)[:max_investigations]
        results: dict[str, Investigation] = {}
        for flag in targets:
            results[flag.key()] = self.investigator.investigate(flag, on_event=on_event)
        return results

    def bu_summary(self, year: int, month: int) -> list[dict]:
        out = []
        for bu in self.data.business_units():
            agg = self.data.aggregate(self.data.slice(year, month, bu))
            tgt, act = agg.get("Target Revenue", 0.0), agg.get("Total Revenue", 0.0)
            out.append({
                "bu": bu,
                "total_revenue": round(act, 0),
                "target_revenue": round(tgt, 0),
                "achievement": round(act / tgt, 4) if tgt else None,
                "cases": round(agg.get("No. Cases", 0.0), 0),
                "target_cases": round(agg.get("Target No. cases", 0.0), 0),
                "leakage": round(agg.get("Total Leakage Revenue Losses", 0.0), 0),
                "cancellation_losses": round(
                    agg.get("Total Losses Revenue_Cancellation_Modification", 0.0), 0),
                "no_show": round(agg.get("No-Show %", 0.0), 4),
            })
        return out

    def run_full_pipeline(self, year: int, month: int, max_investigations: int = 3,
                          on_event=None) -> ExecutiveBrief:
        flags = self.scan(year, month)
        investigations = (self.investigate_flags(flags, max_investigations, on_event)
                          if self.llm.available else {})
        actions = self.recommender.recommend(flags, investigations)
        period_label = f"{calendar.month_name[month]} {year}"
        return self.narrator.compose(period_label, self.bu_summary(year, month),
                                     flags, list(investigations.values()), actions)

    # ------------------------------------------------------------- ad-hoc Q&A
    def ask(self, question: str, history: list[dict] | None = None,
            on_event=None) -> tuple[str, list[dict]]:
        """Conversational agent over the same deterministic tools."""
        context = ""
        if history:
            context = "Previous conversation:\n" + "\n".join(
                f"{m['role']}: {m['content']}" for m in history[-6:]) + "\n\n"
        periods = self.data.periods()
        user = (
            f"{context}Dataset coverage: {periods[0]} to {periods[-1]} "
            f"(Year, Month). Business units: {', '.join(self.data.business_units())}.\n"
            f"Leadership question: {question}"
        )
        return self.llm.run_tool_loop(
            system=CHAT_SYSTEM, user=user, tools=TOOL_SCHEMAS,
            dispatcher=make_dispatcher(self.engine), on_event=on_event,
        )
