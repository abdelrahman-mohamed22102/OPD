"""Stage 3 (Action recommender, deterministic) and Stage 4 (Executive narrator, LLM).

The recommender is a pure join between confirmed causes and the knowledge map's
Recommended_Action / Action_Owner / Escalation_Level — no model in the loop, so
ownership and escalation always come verbatim from governance. The narrator
turns the structured results into a leadership brief, with a template fallback
when no API key is configured.
"""
from __future__ import annotations

import json
import re

from src.domain.models import (
    ActionItem, ExecutiveBrief, Flag, Investigation, Severity,
)
from src.infrastructure.llm_client import GroqClient
from src.infrastructure.repositories import KnowledgeRepository


class ActionRecommender:
    def __init__(self, kb: KnowledgeRepository):
        self.kb = kb

    def recommend(self, flags: list[Flag],
                  investigations: dict[str, Investigation]) -> list[ActionItem]:
        # All distinct BUs present in the scan (used to decide "All" label).
        all_bus = sorted({f.scope["bu"] for f in flags
                          if f.scope.get("bu") and not f.scope.get("doctor")})

        # Step 1 — collect one raw entry per (kpi, scenario, exact_scope).
        # For flags without confirmed causes: use the playbook's scenario-specific
        # recommended_action (more granular than the knowledge map's single generic action).
        raw: list[dict] = []
        seen: set[tuple] = set()
        for flag in flags:
            inv = investigations.get(flag.key())
            scope = self._scope_label(flag)
            bu = flag.scope.get("bu") or "All"
            doctor = flag.scope.get("doctor")
            causes = inv.confirmed_causes if inv and inv.confirmed_causes else None
            if causes:
                for cause in causes:
                    d = self.kb.definition_by_name(cause.kpi)
                    key = (cause.kpi, scope)
                    if key in seen:
                        continue
                    seen.add(key)
                    raw.append(dict(
                        cause_kpi=cause.kpi, bu=bu, doctor=doctor,
                        recommended_action=d.recommended_action if d else "Investigate with function owner",
                        action_owner=d.action_owner if d else "OPD Manager",
                        escalation_level=d.escalation_level if d else "Operations Director",
                        severity=flag.severity,
                        estimated_impact_egp=cause.estimated_impact_egp or flag.estimated_impact_egp,
                        evidence=cause.evidence,
                    ))
            else:
                # Scenario-specific playbook lookup first; knowledge map as fallback.
                pb = self.kb.playbook_rule_for(flag.kpi_name, flag.scenario)
                d = self.kb.definition_by_name(flag.kpi_name)
                key = (flag.kpi_name, flag.scenario, scope)
                if key in seen:
                    continue
                seen.add(key)
                raw.append(dict(
                    cause_kpi=flag.kpi_name, bu=bu, doctor=doctor,
                    recommended_action=(pb.recommended_action if pb
                                        else d.recommended_action if d
                                        else "Investigate with function owner"),
                    action_owner=(d.action_owner if d else "OPD Manager"),
                    escalation_level=(pb.escalation if pb
                                      else d.escalation_level if d
                                      else "Operations Director"),
                    severity=flag.severity,
                    estimated_impact_egp=flag.estimated_impact_egp,
                    evidence=flag.detail,
                ))

        # Step 2 — group BU-level actions that share the same recommended action.
        # Doctor-level actions keep a per-doctor group key so they stay separate.
        groups: dict[tuple, dict] = {}
        for r in raw:
            if r["doctor"]:
                # Doctor-specific — group per doctor scope (no cross-BU merge).
                gkey = (r["cause_kpi"], r["recommended_action"],
                        r["action_owner"], r["escalation_level"],
                        f"{r['bu']}/Dr.{r['doctor']}")
            else:
                # BU-level — merge across BUs if same action & owner.
                gkey = (r["cause_kpi"], r["recommended_action"],
                        r["action_owner"], r["escalation_level"], None)

            if gkey not in groups:
                groups[gkey] = dict(
                    cause_kpi=r["cause_kpi"], bus=[], doctor=r["doctor"],
                    recommended_action=r["recommended_action"],
                    action_owner=r["action_owner"], escalation_level=r["escalation_level"],
                    severity=r["severity"], estimated_impact_egp=0.0,
                    evidence=r["evidence"],
                )
            g = groups[gkey]
            if r["bu"] not in g["bus"]:
                g["bus"].append(r["bu"])
            g["estimated_impact_egp"] += r["estimated_impact_egp"]
            if r["severity"].rank < g["severity"].rank:   # keep most severe
                g["severity"] = r["severity"]

        # Step 3 — build ActionItems; derive BU display label.
        actions: list[ActionItem] = []
        for g in groups.values():
            bus_sorted = sorted(g["bus"])
            if g["doctor"]:
                bu_label = f"{bus_sorted[0]} / Dr. {g['doctor']}"
            elif set(bus_sorted) >= set(all_bus):
                bu_label = "All"
            elif len(bus_sorted) == 1:
                bu_label = bus_sorted[0]
            else:
                bu_label = ", ".join(bus_sorted)

            actions.append(ActionItem(
                cause_kpi=g["cause_kpi"], affected_scope=bu_label,
                recommended_action=g["recommended_action"],
                action_owner=g["action_owner"], escalation_level=g["escalation_level"],
                severity=g["severity"],
                estimated_impact_egp=round(g["estimated_impact_egp"], 0),
                evidence=g["evidence"],
            ))

        # Sort by severity (Critical first), then by EGP descending within each tier.
        actions.sort(key=lambda a: (a.severity.rank, -a.estimated_impact_egp))
        return actions

    @staticmethod
    def _scope_label(flag: Flag) -> str:
        bu = flag.scope.get("bu") or "All BUs"
        doctor = flag.scope.get("doctor")
        return f"{bu} / Dr. {doctor}" if doctor else bu


NARRATOR_SYSTEM = """You write the monthly OPD Financial Outcome brief for executive \
leadership of Andalusia Group (CEO and directors). Formal, concise, decision-oriented \
PMO tone. Currency is EGP.

Structure (markdown):
# OPD Financial Outcome Brief — {period}
## 1. Executive summary  (3-5 sentences: overall position, biggest risk, biggest opportunity)
## 2. Clinic progress  (per BU: revenue achievement vs target, cases, leakage — one line each)
## 3. Indicators decreasing revenue  (all flags ranked by severity then EGP, with root cause chain)
## 4. Recommended actions  (table with columns: Severity | Action | BU | Owner | Escalation | Est. EGP impact)

MANDATORY rules for section 4:
- Include EVERY item from the `actions` array in the input JSON — do NOT omit, merge, or summarise any row.
- Copy the `recommended_action` text VERBATIM from the JSON — do not paraphrase.
- Copy `affected_scope` → BU column, `action_owner` → Owner, `escalation_level` → Escalation, \
`estimated_impact_egp` → Est. EGP impact (round to thousands, show 0 if zero).
- Order rows exactly as supplied (severity desc, then EGP desc within each severity).
- Use ONLY the numbers provided — never invent figures.
- Do not pad; leadership reads this in two minutes."""


class ExecutiveNarrator:
    def __init__(self, llm: GroqClient):
        self.llm = llm

    def compose(self, period_label: str, bu_summary: list[dict], flags: list[Flag],
                investigations: list[Investigation], actions: list[ActionItem]) -> ExecutiveBrief:
        payload = {
            "period": period_label,
            "bu_summary": bu_summary,
            "flags": [{
                "kpi": f.kpi_name, "scenario": f.scenario, "severity": f.severity.value,
                "scope": f.scope, "actual": f.actual, "reference": f.reference,
                "estimated_impact_egp": f.estimated_impact_egp, "detail": f.detail,
            } for f in flags],
            "investigations": [{
                "flag": inv.flag_key, "summary": inv.summary,
                "confirmed_causes": [c.__dict__ for c in inv.confirmed_causes],
                "manual_checks": [c.__dict__ for c in inv.manual_checks],
            } for inv in investigations],
            "actions": [a.__dict__ for a in actions],
        }
        if self.llm.available:
            markdown = self.llm.complete(
                system=NARRATOR_SYSTEM,
                user=json.dumps(payload, default=str),
            )
            # Replace section 4 with a deterministic table — LLMs truncate long tables.
            markdown = self._inject_actions_table(markdown, actions)
        else:
            markdown = self._template_brief(period_label, bu_summary, flags, actions)
        return ExecutiveBrief(period_label=period_label, markdown=markdown,
                              flags=flags, investigations=investigations, actions=actions)

    @staticmethod
    def _actions_table(actions: list[ActionItem]) -> str:
        priority = [a for a in actions if a.severity.value in ("Critical", "High")]
        lines = [
            "## 4. Recommended actions",
            "",
            "_Showing Critical and High severity actions only._",
            "",
            "| Severity | Action | BU | Owner | Escalation | Est. EGP impact | Detail |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for a in priority:
            egp = f"EGP {a.estimated_impact_egp:,.0f}" if a.estimated_impact_egp else "EGP 0"
            detail = (a.evidence or "").replace("|", "\\|")
            lines.append(
                f"| {a.severity.value} | {a.recommended_action} | {a.affected_scope} "
                f"| {a.action_owner} | {a.escalation_level} | {egp} | {detail} |"
            )
        return "\n".join(lines)

    @staticmethod
    def _inject_actions_table(markdown: str, actions: list[ActionItem]) -> str:
        """Replace whatever section 4 the LLM wrote with the deterministic table."""
        table = ExecutiveNarrator._actions_table(actions)
        # Match "## 4. Recommended actions" through the next "## 5." heading (or end of string).
        replaced = re.sub(
            r"##\s*4\..*?(?=##\s*5\.|$)",
            table + "\n\n",
            markdown,
            flags=re.DOTALL | re.IGNORECASE,
        )
        # If the LLM skipped section 4 entirely, append it before section 5 or at end.
        if replaced == markdown:
            replaced = re.sub(
                r"(##\s*5\.)",
                table + "\n\n" + r"\1",
                markdown,
                flags=re.IGNORECASE,
            )
            if replaced == markdown:
                replaced = markdown.rstrip() + "\n\n" + table
        return replaced

    # ------------------------------------------------------- no-LLM fallback
    @staticmethod
    def _template_brief(period_label: str, bu_summary: list[dict],
                        flags: list[Flag], actions: list[ActionItem]) -> str:
        lines = [f"# OPD Financial Outcome Brief — {period_label}",
                 "_Generated without LLM narration (no API key configured)._", "",
                 "## Clinic progress"]
        for b in bu_summary:
            lines.append(
                f"- **{b['bu']}**: revenue EGP {b['total_revenue']:,.0f} vs target "
                f"EGP {b['target_revenue']:,.0f} ({b['achievement']:.1%}); "
                f"leakage EGP {b['leakage']:,.0f}; cancellation losses EGP {b['cancellation_losses']:,.0f}.")
        lines += ["", "## Top flags (ranked by severity, then EGP impact)"]
        for f in flags[:15]:
            scope = f.scope.get("bu") or "ALL"
            if f.scope.get("doctor"):
                scope += f" / Dr. {f.scope['doctor']}"
            lines.append(f"- [{f.severity.value}] **{f.kpi_name}** ({f.scenario}) "
                         f"— {scope}. {f.detail} Est. EGP {f.estimated_impact_egp:,.0f}.")
        lines += ["", "## Recommended actions",
                  "| Severity | Action | BU | Owner | Escalation | Est. EGP impact | Detail |",
                  "| --- | --- | --- | --- | --- | --- | --- |"]
        for a in actions:
            detail = (a.evidence or "").replace("|", "\\|")
            egp = f"EGP {a.estimated_impact_egp:,.0f}" if a.estimated_impact_egp else "EGP 0"
            lines.append(
                f"| {a.severity.value} | {a.recommended_action} | {a.affected_scope} "
                f"| {a.action_owner} | {a.escalation_level} | {egp} | {detail} |"
            )
        return "\n".join(lines)
