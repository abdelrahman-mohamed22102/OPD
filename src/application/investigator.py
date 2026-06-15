"""Root-cause investigator — Stage 2, LLM with tools.

For each Flag, the agent walks the knowledge base's Effect -> Driver -> Cause
relationship map in Investigation_Order, testing each hypothesis against the
deterministic tools. Drivers that are not measurable in the dataset are never
guessed at — they are routed to manual checks with the responsible owner.
"""
from __future__ import annotations

import json
import re

from src.application.kpi_engine import KPIEngine
from src.application.tools import TOOL_SCHEMAS, make_dispatcher
from src.domain.models import CauseFinding, Flag, Investigation
from src.infrastructure.llm_client import GroqClient

SYSTEM_PROMPT = """You are the Root-Cause Investigator for Andalusia Group OPD. \
The knowledge-base entry for the flagged KPI is already in the user message — \
do NOT call kb_lookup for it again.

Rules:
1. Never compute numbers yourself — every figure must come from a tool result.
2. Investigate children in their Investigation_Order, highest weight first.
3. For measurable children use kpi_trend or rank_contributors (not kpi_snapshot — too broad).
4. measurable_in_dataset=false → record as manual_check with owner, never query data.
5. Use rank_contributors(dimension="Doctor") only when a BU-level cause is confirmed.
6. Stop after 4-6 tool calls once dominant causes are clear.

Output ONLY a JSON object (no markdown):
{
  "summary": "4-6 sentence business diagnosis: state what is wrong and by how much, \
explain what the numbers mean in operational terms, identify the dominant cause chain \
(Effect → Driver → Cause) with the figures that confirm it, and state the EGP exposure.",
  "confirmed_causes": [{
    "kpi": "...",
    "evidence": "Specific numbers from tools + what they imply operationally \
(e.g. 'dropped from X to Y MoM, accounting for EGP Z of the shortfall because ...')",
    "estimated_impact_egp": 0
  }],
  "ruled_out": [{"kpi": "...", "evidence": "numbers that show this is within normal range and why"}],
  "manual_checks": [{"kpi": "...", "evidence": "what needs to be verified and why it matters", "owner": "..."}]
}"""


class RootCauseInvestigator:
    def __init__(self, engine: KPIEngine, llm: GroqClient):
        self.engine = engine
        self.llm = llm
        self.dispatch = make_dispatcher(engine)

    def investigate(self, flag: Flag, on_event=None) -> Investigation:
        # Pre-fetch kb entry so the LLM skips that round trip entirely.
        kb_data = self.dispatch("kb_lookup", {"kpi_name": flag.kpi_name})
        flag_dict = {
            "kpi": flag.kpi_name, "kpi_id": flag.kpi_id,
            "scenario": flag.scenario, "severity": flag.severity.value,
            "scope": flag.scope, "actual": flag.actual,
            "reference": flag.reference,
            "estimated_impact_egp": flag.estimated_impact_egp,
            "detail": flag.detail,
        }
        user = (
            f"Flag: {json.dumps(flag_dict, default=str)}\n\n"
            f"Knowledge base for {flag.kpi_name}:\n{json.dumps(kb_data, default=str)}\n\n"
            "Investigate and report your structured findings."
        )
        text, trace = self.llm.run_tool_loop(
            system=SYSTEM_PROMPT, user=user, tools=TOOL_SCHEMAS,
            dispatcher=self.dispatch, on_event=on_event,
        )
        parsed = self._parse(text)
        return Investigation(
            flag_key=flag.key(),
            summary=parsed.get("summary", text[:500]),
            confirmed_causes=[self._finding(c, "confirmed") for c in parsed.get("confirmed_causes", [])],
            ruled_out=[self._finding(c, "ruled_out") for c in parsed.get("ruled_out", [])],
            manual_checks=[self._finding(c, "needs_manual_check") for c in parsed.get("manual_checks", [])],
            trace=trace,
        )

    @staticmethod
    def _parse(text: str) -> dict:
        """Tolerant JSON extraction (the model may wrap output in prose/fences)."""
        candidate = text.strip()
        match = re.search(r"\{.*\}", candidate, flags=re.DOTALL)
        if match:
            candidate = match.group(0)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            return {"summary": text.strip()}

    @staticmethod
    def _finding(raw: dict, verdict: str) -> CauseFinding:
        impact = raw.get("estimated_impact_egp")
        try:
            impact = float(impact) if impact is not None else None
        except (TypeError, ValueError):
            impact = None
        evidence = raw.get("evidence", "")
        if verdict == "needs_manual_check" and raw.get("owner"):
            evidence = f"{evidence} (owner: {raw['owner']})"
        return CauseFinding(kpi=str(raw.get("kpi", "Unknown")), verdict=verdict,
                            evidence=evidence, estimated_impact_egp=impact)
