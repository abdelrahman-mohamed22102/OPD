"""Deterministic KPI engine — the tool layer.

Every number the agents reason about is computed here in plain pandas, never
by the LLM. All functions return JSON-serializable dicts so they can be
exposed directly as Anthropic tool results.
"""
from __future__ import annotations

import pandas as pd

from src.infrastructure.repositories import DatasetRepository, KnowledgeRepository


class KPIEngine:
    def __init__(self, data: DatasetRepository, kb: KnowledgeRepository):
        self.data = data
        self.kb = kb

    # ------------------------------------------------------------------ utils
    def _resolve(self, kpi_name: str) -> str | None:
        return self.kb.resolve_column(kpi_name)

    @staticmethod
    def _prev_period(year: int, month: int) -> tuple[int, int]:
        return (year - 1, 12) if month == 1 else (year, month - 1)

    # ------------------------------------------------------------------ tools
    def kpi_snapshot(self, year: int, month: int,
                     bu: str | None = None, doctor: str | None = None) -> dict:
        """KPIs for one period/scope (non-zero only), with prior-month comparison."""
        cur = self.data.aggregate(self.data.slice(year, month, bu, doctor))
        py, pm = self._prev_period(year, month)
        prev = self.data.aggregate(self.data.slice(py, pm, bu, doctor))
        # Strip zero/NaN entries — they add noise without insight.
        def _trim(d: dict) -> dict:
            return {k: round(v, 4) for k, v in d.items()
                    if pd.notna(v) and v != 0.0}
        return {
            "scope": {"year": year, "month": month, "bu": bu or "ALL", "doctor": doctor or "ALL"},
            "current": _trim(cur),
            "previous_month": _trim(prev),
        }

    def kpi_trend(self, kpi_name: str, n_months: int = 6,
                  bu: str | None = None, doctor: str | None = None,
                  end_year: int | None = None, end_month: int | None = None) -> dict:
        col = self._resolve(kpi_name)
        if col is None:
            return {"error": f"'{kpi_name}' is not measurable in the dataset.",
                    "measurable": False}
            
        periods = self.data.periods()
        
        if end_year and end_month: #py , Pm
            periods = [p for p in periods if p <= (end_year, end_month)]
            
        periods = periods[-n_months:]
        series = []
        
        for y, m in periods:
            agg = self.data.aggregate(self.data.slice(y, m, bu, doctor))
            series.append({"year": y, "month": m, "value": round(agg.get(col, float("nan")), 4)})
        
        values = [p["value"] for p in series if pd.notna(p["value"])]
        
        return {
            "kpi": col, "scope": {"bu": bu or "ALL", "doctor": doctor or "ALL"},
            "series": series,
            "mean": round(sum(values) / len(values), 4) if values else None,
            "min": round(min(values), 4) if values else None,
            "max": round(max(values), 4) if values else None,
        }

    def rank_contributors(self, kpi_name: str, dimension: str,
                          year: int, month: int, bu: str | None = None,
                          top_n: int = 5) -> dict:
        """Who is driving the gap? Ranks BU or Doctor by variance vs target
        (when a target exists) or by current vs prior-month delta otherwise."""
        col = self._resolve(kpi_name)
        if col is None:
            return {"error": f"'{kpi_name}' is not measurable in the dataset.",
                    "measurable": False}
            
        dim_col = {"BU": "BU", "Doctor": "Doctor Name"}.get(dimension)
        if dim_col is None:
            return {"error": "dimension must be 'BU' or 'Doctor'"}

        target_col = {"Total Revenue": "Target Revenue", "No. Cases": "Target No. cases"}.get(col)
        rows = []
        
        members = (self.data.business_units() if dim_col == "BU"
                   else self.data.doctors(bu))
        
        
        for member in members:
            kwargs = {"bu": member} if dim_col == "BU" else {"bu": bu, "doctor": member}
            cur = self.data.aggregate(self.data.slice(year, month, **kwargs))
            
            entry = {"member": member, "actual": round(cur.get(col, 0.0), 2)}
            
            if target_col:#if the KPI has a target column, compute variance vs target
                tgt = cur.get(target_col, 0.0)
                entry["target"] = round(tgt, 2)
                entry["variance"] = round(cur.get(col, 0.0) - tgt, 2)
                entry["achievement_pct"] = round(cur.get(col, 0.0) / tgt, 4) if tgt else None
            else:#if no target column, compute variance vs prior month
                py, pm = self._prev_period(year, month)
                prev = self.data.aggregate(self.data.slice(py, pm, **kwargs))
                entry["previous"] = round(prev.get(col, 0.0), 4)
                entry["variance"] = round(cur.get(col, 0.0) - prev.get(col, 0.0), 4)
                
                
            rows.append(entry)
            
            
        rows.sort(key=lambda r: r.get("variance") or 0)
        
        return {"kpi": col, "dimension": dimension,
                "scope": {"year": year, "month": month, "bu": bu or "ALL"},
                "ranked_worst_first": rows[:top_n]}



    def quantify_impact(self, year: int, month: int,
                        bu: str | None = None, doctor: str | None = None) -> dict:
        """EGP impact of the main loss buckets for a scope, per the KB's
        Financial_Impact_Formula definitions."""
        agg = self.data.aggregate(self.data.slice(year, month, bu, doctor))
        
        revenue_gap = max(agg.get("Target Revenue", 0) - agg.get("Total Revenue", 0), 0.0)
        
        charge = agg.get("Charge per case", 0.0)
        
        return {
            "scope": {"year": year, "month": month, "bu": bu or "ALL", "doctor": doctor or "ALL"},
            "revenue_gap_vs_target_egp": round(revenue_gap, 2),
            "leakage_losses_egp": round(agg.get("Total Leakage Revenue Losses", 0.0), 2),
            
            "cancellation_modification_losses_egp": round(
                agg.get("Total Losses Revenue_Cancellation_Modification", 0.0), 2),
            
            "missed_opportunity_est_egp": round(agg.get("No. Missed Opportunity", 0) * charge, 2),
            
            "no_show_est_egp": round(agg.get("No. Booking", 0) * agg.get("No-Show %", 0) * charge, 2),
        }

    def kb_lookup(self, kpi_name: str) -> dict:
        """Definition, playbook rules, drivers and measurability for one KPI."""
        d = self.kb.definition_by_name(kpi_name)
        if d is None:
            return {"error": f"No knowledge base entry for '{kpi_name}'."}
        children = [
            {"kpi": rel.child, 
             "type": rel.relationship_type, 
             "weight": rel.weight,
             "investigation_order": rel.investigation_order,
             "measurable_in_dataset": rel.measurable,
             "dataset_column": rel.resolved_column}
            
            for rel in self.kb.children_of(d.name)
        ]
        rules = [
            {"scenario": p.scenario, 
             "threshold": p.threshold, 
             "severity": p.severity.value,
             "root_cause_focus": p.root_cause_focus,
             "recommended_action": p.recommended_action, 
             "escalation": p.escalation}
            for p in self.kb.playbook_for(d.name)
        ]
        return {
            "kpi_id": d.kpi_id, 
            "kpi": d.name, 
            "layer": d.layer.value,
            "business_question": d.business_question,
            "formula": d.formula_logic or d.financial_impact_formula,
            "investigation_steps": list(d.investigation_steps),
            "action_owner": d.action_owner, 
            "escalation_level": d.escalation_level,
            "recommended_action": d.recommended_action,
            "children": children, "playbook_rules": rules,
        }
