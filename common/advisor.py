"""A Python port of the refdata_advisor scoring model shown in the product
demo (input.html's Advisor Simulator). There's no refdata_advisor extension
installed on this server -- this reimplements the same scoring/classification
logic, but feeds it real numbers measured by the benchmark harness instead of
slider values.
"""
import math

DEFAULT_GUC = {
    "max_self_dml": 100,
    "min_benefit_ratio": 10,
    "max_row_count": 100_000,
    "multixact_pressure_threshold": 50,
}


def compute_score(self_dml: float, child_dml: float, rows: float, mxid_pct: float) -> float:
    if child_dml == 0:
        return 0.0
    ratio = child_dml / max(self_dml, 1)
    volume_factor = math.log(max(child_dml, 1)) + 1
    size_factor = 1 / (math.log(max(rows, 2)) + 1)
    if mxid_pct < 10:
        mxid_boost = 1.0
    elif mxid_pct < 30:
        mxid_boost = 1.25
    elif mxid_pct < 50:
        mxid_boost = 1.5
    elif mxid_pct < 75:
        mxid_boost = 2.0
    else:
        mxid_boost = 2.75
    return ratio * volume_factor * size_factor * mxid_boost


def mxid_pressure_label(pct: float) -> str:
    if pct < 10:
        return "none"
    if pct < 30:
        return "low"
    if pct < 50:
        return "moderate"
    if pct < 75:
        return "high"
    return "critical"


def classify(self_dml: float, benefit_ratio: float, rows: float, am: str,
             mxid_pct: float, guc: dict = None) -> str:
    guc = guc or DEFAULT_GUC
    max_self_dml = guc["max_self_dml"]
    min_ratio = guc["min_benefit_ratio"]
    max_rows = guc["max_row_count"]
    mx_threshold = guc["multixact_pressure_threshold"]

    if am == "refdata":
        if self_dml > max_self_dml * 5 and benefit_ratio < min_ratio:
            return "switch_to_heap"
        return "already_refdata"

    if self_dml <= max_self_dml and benefit_ratio >= min_ratio * 2 and rows <= max_rows:
        base = "strongly_recommended"
    elif self_dml <= max_self_dml * 5 and benefit_ratio >= min_ratio and rows <= max_rows * 5:
        base = "recommended"
    else:
        base = "neutral"

    if mxid_pct >= mx_threshold and benefit_ratio >= 1.0:
        if base == "neutral":
            return "recommended"
        if base == "recommended":
            return "strongly_recommended"
    return base


def recommend(qualified_name: str, self_dml: int, child_dml: int, rows: int,
              mxid_pct: float, am: str = "heap", guc: dict = None) -> dict:
    ratio = child_dml / max(self_dml, 1)
    score = compute_score(self_dml, child_dml, rows, mxid_pct)
    rec = classify(self_dml, ratio, rows, am, mxid_pct, guc)

    alter_statement = None
    if rec in ("strongly_recommended", "recommended"):
        alter_statement = f"ALTER TABLE {qualified_name} SET ACCESS METHOD refdata;"
    elif rec == "switch_to_heap":
        alter_statement = f"ALTER TABLE {qualified_name} SET ACCESS METHOD heap;"

    return {
        "table": qualified_name,
        "rows": rows,
        "self_dml": self_dml,
        "child_dml": child_dml,
        "benefit_ratio": ratio,
        "mxid_pct": mxid_pct,
        "mxid_pressure": mxid_pressure_label(mxid_pct),
        "score": score,
        "recommendation": rec,
        "alter_statement": alter_statement,
    }


def header_line(rec: dict) -> str:
    status = rec["recommendation"].replace("_", " ")
    return f"== Workflow `refdata` recommendation: {status} =="


def print_report(rec: dict):
    print(f"  table:              {rec['table']}")
    print(f"  rows:               {rec['rows']:,}")
    print(f"  self DML:           {rec['self_dml']:,}  (writes to the table itself)")
    print(f"  child FK DML:       {rec['child_dml']:,}  (writes to tables referencing it)")
    print(f"  benefit ratio:      {rec['benefit_ratio']:,.1f}")
    print(f"  MultiXact pressure: {rec['mxid_pct']:.1f}% ({rec['mxid_pressure']})")
    print(f"  advisor score:      {rec['score']:,.1f}")
    print(f"  recommendation:     {rec['recommendation'].replace('_', ' ').upper()}")
    if rec["alter_statement"]:
        print(f"  {rec['alter_statement']}")
