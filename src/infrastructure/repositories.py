"""Infrastructure layer: loading the OPD dataset and the KPI knowledge base.

Everything that knows about Excel sheet names and column quirks lives here, so
the application layer only ever sees clean DataFrames and domain objects.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pandas as pd

from src.config import KPI_NAME_ALIASES
from src.domain.models import (
    KPIDefinition,
    KPILayer,
    KPIRelationship,
    PlaybookRule,
    Severity,
)

DIMENSIONS = {"Year", "Month No", "Month", "BU", "Doctor Name"}

# KPIs that are ratios/percentages: aggregate with mean, not sum.
RATIO_COLUMNS = {
    "Doctor PMS %", "Service Leakage %", "Cross Referral %", "Patient Retention %",
    "Patient Acquisition %", "Actual COE Compliance %", "Digital Actual CR%",
    "Digital Target CR%", "No-Show %",
}


class DatasetRepository:
    """Owns the OPD fact table."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._df: pd.DataFrame | None = None

    @property
    def df(self) -> pd.DataFrame:
        if self._df is None:
            df = pd.read_excel(self.path, engine="openpyxl")
            df.columns = [str(c).strip() for c in df.columns]
            self._df = df
        return self._df

    @property
    def kpi_columns(self) -> list[str]:
        return [c for c in self.df.columns if c not in DIMENSIONS]

    def periods(self) -> list[tuple[int, int]]:
        p = self.df[["Year", "Month No"]].drop_duplicates().sort_values(["Year", "Month No"])
        return [tuple(map(int, row)) for row in p.to_numpy()]

    def business_units(self) -> list[str]:
        return sorted(self.df["BU"].dropna().unique().tolist())

    #if select BU Return all doctors in that BU, else return all doctors
    def doctors(self, bu: str | None = None) -> list[str]:
        df = self.df if bu is None else self.df[self.df["BU"] == bu]
        return sorted(df["Doctor Name"].dropna().unique().tolist())

    def slice(self, year: int | None = None, month: int | None = None,
              bu: str | None = None, doctor: str | None = None) -> pd.DataFrame:
        df = self.df
        if year is not None:
            df = df[df["Year"] == year]
        if month is not None:
            df = df[df["Month No"] == month]
        if bu:
            df = df[df["BU"] == bu]
        if doctor:
            df = df[df["Doctor Name"] == doctor]
        return df

    def aggregate(self, df: pd.DataFrame) -> dict[str, float]:
        """Aggregate a slice into one KPI dict (sum for amounts, mean for ratios,
        recompute charge-per-case)."""
        out: dict[str, float] = {}
        for col in self.kpi_columns:
            if col not in df.columns or df.empty:
                continue
            series = pd.to_numeric(df[col], errors="coerce").dropna()
            if series.empty:
                continue
            out[col] = float(series.mean() if col in RATIO_COLUMNS else series.sum())
        # Charge per case is a ratio of sums, not a sum of ratios.
        if out.get("No. Cases"):
            out["Charge per case"] = out.get("Total Revenue", 0.0) / out["No. Cases"]
        return out


class KnowledgeRepository:
    """Owns the six knowledge-base sheets and resolves KPI name aliases."""

    SHEET_PREFIXES = {
        "knowledge_map": "adx_kpi_knowledge_map",
        "relationship_map": "adx_kpi_relationship_map",
        "formula_definition": "adx_kpi_formula_definition",
        "playbook": "adx_kpi_investigation_playbook",
        "filter_compat": "adx_kpi_filter_compatibility",
        "scope": "adx_dim_kpi_scope",
    }

    def __init__(self, path: Path, dataset_columns: list[str]):
        self.path = Path(path)
        self._dataset_columns = set(dataset_columns)
        self._sheets: dict[str, pd.DataFrame] = {}
        self._load()

    def _load(self) -> None:
        xl = pd.ExcelFile(self.path, engine="openpyxl")
        for key, prefix in self.SHEET_PREFIXES.items():
            match = next((s for s in xl.sheet_names if s.startswith(prefix)), None)
            if match is None:
                raise ValueError(f"Knowledge base is missing a sheet starting with '{prefix}'")
            df = xl.parse(match)
            df.columns = [str(c).strip() for c in df.columns]
            self._sheets[key] = df

    # ---- name resolution -------------------------------------------------
    def resolve_column(self, kpi_name: str) -> str | None:
        """Map a KB KPI name to a dataset column, or None if not measurable."""
        name = str(kpi_name).strip()
        if name in self._dataset_columns:
            return name
        alias = KPI_NAME_ALIASES.get(name)
        if alias and alias in self._dataset_columns:
            return alias
        return None

    # ---- domain object accessors ------------------------------------------
    @lru_cache(maxsize=None)
    def definitions(self) -> dict[str, KPIDefinition]:
        km = self._sheets["knowledge_map"]
        formulas = self._sheets["formula_definition"].set_index("KPI_ID")
        scope = self._sheets["scope"].set_index("KPI_ID")
        defs: dict[str, KPIDefinition] = {}
        
        for _, r in km.iterrows():
            kid = r["KPI_ID"]
            
            defs[r["KPI_Name"]] = KPIDefinition(
                kpi_id=kid,
                name=r["KPI_Name"],
                
                layer=KPILayer(r["KPI_Layer"]),
                
                owner_role=r["KPI_Owner_Role"],
                function_owner=r["Function_Owner"],
                business_question=r["Business_Question"],
                financial_impact_formula=r["Financial_Impact_Formula"],
                primary_driver=r["Primary_Driver_KPI"],
                secondary_driver=r["Secondary_Driver_KPI"],
                
                investigation_steps=tuple(
                    str(r[f"Investigation_Step_{i}"]) for i in range(1, 5)
                    if pd.notna(r.get(f"Investigation_Step_{i}"))
                ),
                
                action_owner=r["Action_Owner"],
                escalation_level=r["Escalation_Level"],
                recommended_action=r["Recommended_Action"],
                
                formula_logic=str(formulas.loc[kid, "Formula_Logic"]) if kid in formulas.index else "",
                lowest_granularity=str(scope.loc[kid, "Lowest_Granularity"]) if kid in scope.index else "",
                not_available_message=str(scope.loc[kid, "Not_Available_Message"]) if kid in scope.index else "",
            )
        return defs
    
    
    def definition_by_name(self, kpi_name: str) -> KPIDefinition | None:
        defs = self.definitions()
        if kpi_name in defs:
            return defs[kpi_name]
        # tolerate alias direction (dataset column -> KB name)
        for d in defs.values():
            if self.resolve_column(d.name) == kpi_name:
                return d
        return None
    
    
    @lru_cache(maxsize=None)
    def relationships(self) -> list[KPIRelationship]:
        rm = self._sheets["relationship_map"]
        rels: list[KPIRelationship] = []
        
        for _, r in rm.iterrows():
            col = self.resolve_column(r["Child_KPI"])
            
            rels.append(
                KPIRelationship(
                    parent=str(r["Parent_KPI"]).strip(),
                    child=str(r["Child_KPI"]).strip(),
                    relationship_type=str(r["Relationship_Type"]),
                    weight=str(r["Weight"]),
                    investigation_order=int(r["Investigation_Order"]),
                    #################################
                    measurable=col is not None,#Bolean: is the child KPI resolvable to a dataset column?
                    resolved_column=col,#resolved dataset column name for the child KPI (or None if not measurable
            ))
        return rels

    def children_of(self, parent_kpi: str) -> list[KPIRelationship]:
        return sorted(
            (rel for rel in self.relationships() if rel.parent == parent_kpi),
            key=lambda rel: rel.investigation_order,
        )



    @lru_cache(maxsize=None)
    def playbook(self) -> list[PlaybookRule]:
        pb = self._sheets["playbook"]
        rules: list[PlaybookRule] = []
        for _, r in pb.iterrows():
            try:
                sev = Severity(str(r["Severity"]).strip().title())
            except ValueError:
                sev = Severity.MEDIUM
            rules.append(
                PlaybookRule(
                    kpi_id=r["KPI_ID"], 
                    kpi_name=r["KPI"], 
                    scenario=r["Scenario"],
                    threshold=str(r["Threshold"]), 
                    
                    severity=sev,
                    
                    root_cause_focus=str(r["Root_Cause_Focus"]),
                    recommended_investigation=str(r["Recommended_Investigation"]),
                    recommended_action=str(r["Recommended_Action"]),
                    escalation=str(r["Escalation"]),
            ))
        return rules

    def playbook_for(self, kpi_name: str) -> list[PlaybookRule]:
        return [p for p in self.playbook() if p.kpi_name == kpi_name]

    def playbook_rule_for(self, kpi_name: str, scenario: str) -> PlaybookRule | None:
        """Exact match on KPI name + scenario string (used by ActionRecommender)."""
        return next(
            (p for p in self.playbook() if p.kpi_name == kpi_name and p.scenario == scenario),
            None,
        )


