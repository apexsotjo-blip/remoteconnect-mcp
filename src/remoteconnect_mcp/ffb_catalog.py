"""Function / function-block catalog for FBD authoring.

Three sources, merged:
* EFBs (stateful library blocks: TON, CTU, SR, PID, ...) — `ffb_catalog.json`, generated
  by tools/build_ffb_catalog.py from the installed libset through UDE
  (IEfb.ListSubfields on declared instances), so pins/types are exact for that install.
* EFs (elementary functions: AND_BOOL, ADD, GT, SEL, INT_TO_REAL, ...) — the table below.
  UDE does not list functions, so these follow IEC 61131-3 / Control Expert pin names,
  each checked by an import + build (tools/build_ffb_catalog.py --verify-efs).
* DFBs — read live from the open project by the bridge.
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache

from .fbd_builder import BlockSpec, Pin

CATALOG_FILE = os.path.join(os.path.dirname(__file__), "ffb_catalog.json")
NATURE = {"3": "in", "4": "out", "5": "inout"}          # IEfb.ListSubfields nature codes; 2 = internal

_INT_TYPES = ("INT", "DINT", "UINT", "UDINT", "REAL")


def _ef(name, ins, outs, family, desc, ext=None, min_inputs=2):
    return {"name": name, "kind": "EF", "family": family, "description": desc,
            "pins": [{"name": n, "type": t, "dir": "in"} for n, t in ins]
                    + [{"name": n, "type": t, "dir": "out"} for n, t in outs],
            "extensible": ext, "min_inputs": min_inputs}


def _elementary() -> list[dict]:
    efs = []
    for op, d in (("AND", "logical AND"), ("OR", "logical OR"), ("XOR", "exclusive OR")):
        efs.append(_ef(f"{op}_BOOL", [("IN1", "BOOL"), ("IN2", "BOOL")], [("OUT", "BOOL")], "Logic",
                       f"{d} of 2..n BOOL inputs (add IN3, IN4, ... to extend)", ext="IN"))
        efs.append(_ef(op, [("IN1", "ANY_BIT"), ("IN2", "ANY_BIT")], [("OUT", "ANY_BIT")], "Logic",
                       f"bitwise {d} of 2..n inputs", ext="IN"))
    efs.append(_ef("NOT_BOOL", [("IN", "BOOL")], [("OUT", "BOOL")], "Logic", "logical NOT"))
    for op, d, ext in (("ADD", "sum", "IN"), ("MUL", "product", "IN"), ("SUB", "IN1 - IN2", None),
                       ("DIV", "IN1 / IN2", None)):
        ins = [("IN1", "ANY_NUM"), ("IN2", "ANY_NUM")]
        efs.append(_ef(op, ins, [("OUT", "ANY_NUM")], "Arithmetic", f"{d} (generic numeric)", ext=ext))
        for t in _INT_TYPES:
            efs.append(_ef(f"{op}_{t}", [("IN1", t), ("IN2", t)], [("OUT", t)], "Arithmetic",
                           f"{d} of {t}", ext=ext))
    efs.append(_ef("MOD", [("IN1", "ANY_INT"), ("IN2", "ANY_INT")], [("OUT", "ANY_INT")], "Arithmetic",
                   "remainder of IN1 / IN2"))
    for fn, d in (("ABS", "absolute value"), ("SQRT", "square root"), ("NEG", "negation")):
        efs.append(_ef(fn, [("IN", "ANY_NUM")], [("OUT", "ANY_NUM")], "Arithmetic", d))
    for op, d in (("GT", ">"), ("GE", ">="), ("EQ", "="), ("NE", "<>"), ("LE", "<="), ("LT", "<")):
        efs.append(_ef(op, [("IN1", "ANY_ELEM"), ("IN2", "ANY_ELEM")], [("OUT", "BOOL")], "Comparison",
                       f"IN1 {d} IN2"))
    efs.append(_ef("MOVE", [("IN", "ANY")], [("OUT", "ANY")], "Selection", "copy IN to OUT"))
    efs.append(_ef("SEL", [("G", "BOOL"), ("IN0", "ANY"), ("IN1", "ANY")], [("OUT", "ANY")], "Selection",
                   "OUT = IN0 when G is FALSE, IN1 when G is TRUE"))
    efs.append(_ef("LIMIT", [("MN", "ANY_ELEM"), ("IN", "ANY_ELEM"), ("MX", "ANY_ELEM")],
                   [("OUT", "ANY_ELEM")], "Selection", "clamp IN between MN and MX"))
    for fn, d in (("MAX", "largest"), ("MIN", "smallest")):
        efs.append(_ef(fn, [("IN1", "ANY_ELEM"), ("IN2", "ANY_ELEM")], [("OUT", "ANY_ELEM")], "Selection",
                       f"{d} of 2..n inputs", ext="IN"))
    conv = [("INT", "REAL"), ("REAL", "INT"), ("DINT", "REAL"), ("REAL", "DINT"), ("INT", "DINT"),
            ("DINT", "INT"), ("UINT", "INT"), ("INT", "UINT"), ("UINT", "REAL"), ("REAL", "UINT"),
            ("BOOL", "INT"), ("INT", "BOOL"), ("WORD", "INT"), ("INT", "WORD"), ("TIME", "DINT"),
            ("DINT", "TIME"), ("UDINT", "REAL"), ("REAL", "UDINT"), ("INT", "TIME"), ("TIME", "INT")]
    for a, b in conv:
        efs.append(_ef(f"{a}_TO_{b}", [("IN", a)], [("OUT", b)], "TypeToType", f"convert {a} to {b}"))
    return efs


@lru_cache(maxsize=1)
def _generated() -> dict:
    if not os.path.isfile(CATALOG_FILE):
        return {"blocks": [], "source": "missing — run tools/build_ffb_catalog.py"}
    with open(CATALOG_FILE, encoding="utf-8") as f:
        return json.load(f)


def entries(project_dfbs: list[dict] | None = None) -> list[dict]:
    """All known blocks: generated EFBs, elementary functions, then project DFBs."""
    seen, out = set(), []
    for e in _generated().get("blocks", []) + _elementary() + list(project_dfbs or []):
        if e["name"].upper() in seen:
            continue
        seen.add(e["name"].upper())
        out.append(e)
    return out


def catalog_info() -> dict:
    g = _generated()
    return {k: v for k, v in g.items() if k != "blocks"}


def search(query: str = "", family: str = "", kind: str = "", project_dfbs=None, limit: int = 200) -> dict:
    q = query.lower().strip()
    rows = []
    for e in entries(project_dfbs):
        if kind and e["kind"].upper() != kind.upper():
            continue
        if family and family.lower() not in (e.get("family") or "").lower():
            continue
        hay = f"{e['name']} {e.get('family', '')} {e.get('description', '')}".lower()
        if q and not all(w in hay for w in q.split()):
            continue
        ins = [p["name"] for p in e["pins"] if p["dir"] in ("in", "inout")]
        outs = [p["name"] for p in e["pins"] if p["dir"] in ("out", "inout")]
        rows.append({"name": e["name"], "kind": e["kind"], "family": e.get("family", ""),
                     "description": (e.get("description") or "")[:120],
                     "inputs": ins + ([f"{e['extensible']}…"] if e.get("extensible") else []), "outputs": outs})
    families = sorted({e.get("family", "") for e in entries(project_dfbs)})
    return {"total": len(rows), "returned": min(len(rows), limit), "blocks": rows[:limit],
            "families": families if not (q or family) else None, "catalog": catalog_info()}


def find(name: str, project_dfbs=None) -> dict | None:
    for e in entries(project_dfbs):
        if e["name"].upper() == name.upper():
            return e
    return None


def to_spec(e: dict) -> BlockSpec:
    pins = [Pin(p["name"], p["type"], p["dir"]) for p in e["pins"]]
    return BlockSpec(type=e["name"], kind=e["kind"],
                     inputs=[p for p in pins if p.direction in ("in", "inout")],
                     outputs=[p for p in pins if p.direction in ("out", "inout")],
                     extensible=e.get("extensible"), min_inputs=e.get("min_inputs", 2))


def dfb_entry(name: str, ins, outs, inouts, comment: str = "") -> dict:
    return {"name": name, "kind": "DFB", "family": "Project DFB", "description": comment,
            "pins": [{"name": n, "type": t, "dir": "in"} for n, t in ins]
                    + [{"name": n, "type": t, "dir": "inout"} for n, t in inouts]
                    + [{"name": n, "type": t, "dir": "out"} for n, t in outs]}


def is_generic(type_name: str) -> bool:
    return bool(re.match(r"^ANY", type_name or "", re.I))
