"""Tool definitions exposed to the agents, bound to the deterministic KPIEngine.

Groq uses the OpenAI function-calling schema format:
  {"type": "function", "function": {"name": ..., "parameters": ...}}

The schemas mirror KPIEngine method signatures one-to-one.
"""
from __future__ import annotations

from src.application.kpi_engine import KPIEngine

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "kpi_snapshot",
            "description": "All KPI values for one period and scope, with the prior month "
                           "for comparison. Use this first to ground any investigation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "year": {"type": "integer"},
                    "month": {"type": "integer", "description": "Month number 1-12"},
                    "bu": {"type": "string", "description": "Business unit code (ASH/SMH/HJH). Omit for all."},
                    "doctor": {"type": "string", "description": "Doctor name. Omit for all."},
                },
                "required": ["year", "month"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kpi_trend",
            "description": "Monthly time series for one KPI over the last n months, with "
                           "mean/min/max. Use to judge whether a value is unusual vs its own baseline.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kpi_name": {"type": "string"},
                    "n_months": {"type": "integer", "description": "Default 6"},
                    "bu": {"type": "string", "description": "Business unit code. Omit for all."},
                    "doctor": {"type": "string", "description": "Doctor name. Omit for all."},
                    "end_year": {"type": "integer", "description": "End year. Omit for latest."},
                    "end_month": {"type": "integer", "description": "End month. Omit for latest."},
                },
                "required": ["kpi_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rank_contributors",
            "description": "Rank BUs or Doctors by variance on a KPI (worst first) to find "
                           "who is driving a gap.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kpi_name": {"type": "string"},
                    "dimension": {"type": "string", "enum": ["BU", "Doctor"]},
                    "year": {"type": "integer"},
                    "month": {"type": "integer"},
                    "bu": {"type": "string", "description": "Restrict doctor ranking to one BU. Omit for all."},
                },
                "required": ["kpi_name", "dimension", "year", "month"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "quantify_impact",
            "description": "EGP impact of the main loss buckets (revenue gap, leakage, "
                           "cancellations, missed opportunities, no-show) for a scope.",
            "parameters": {
                "type": "object",
                "properties": {
                    "year": {"type": "integer"},
                    "month": {"type": "integer"},
                    "bu": {"type": "string", "description": "Business unit code. Omit for all."},
                    "doctor": {"type": "string", "description": "Doctor name. Omit for all."},
                },
                "required": ["year", "month"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kb_lookup",
            "description": "Knowledge-base entry for a KPI: definition, formula, investigation "
                           "steps, driver/cause children (with measurability), playbook rules, "
                           "action owner and escalation level. ALWAYS check 'measurable_in_dataset' "
                           "before requesting data for a child KPI.",
            "parameters": {
                "type": "object",
                "properties": {"kpi_name": {"type": "string"}},
                "required": ["kpi_name"],
            },
        },
    },
]


def make_dispatcher(engine: KPIEngine):
    """Bind tool names to KPIEngine methods.

    Groq may omit optional params from the JSON args; we pop and pass None.
    """
    handlers = {
        "kpi_snapshot": engine.kpi_snapshot,
        "kpi_trend": engine.kpi_trend,
        "rank_contributors": engine.rank_contributors,
        "quantify_impact": engine.quantify_impact,
        "kb_lookup": engine.kb_lookup,
    }

    def dispatch(name: str, args: dict) -> dict:
        if name not in handlers:
            return {"error": f"Unknown tool '{name}'"}
        # Normalize missing optional string args to None
        cleaned = {}
        for k, v in args.items():
            if v == "" or v == "null":
                v = None
            cleaned[k] = v
        return handlers[name](**cleaned)

    return dispatch
