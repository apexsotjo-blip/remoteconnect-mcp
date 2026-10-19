"""Fast variable listing: one Variables.Export (one COM call) parsed in Python, then filter + page.

The old list_variables walked every variable over COM (~20 ms per call per property); on IWT's 4,296
variables that took 86 s even to return 200 names, and 224 s / 563 KB to return all. Runs on the
bridge's COM worker (call through bridge._run); shared by both servers."""
from __future__ import annotations

import html
import re
from typing import Any

_VAR = re.compile(r'<variables name="([^"]+)" typeName="([^"]+)"([^>]*)>(.*?)</variables>|'
                  r'<variables name="([^"]+)" typeName="([^"]+)"([^>]*)/>', re.S)
_ATTR = re.compile(r'(\w+)="([^"]*)"')


def parse_variables_export(text: str) -> list[dict[str, Any]]:
    """Rows with the same keys the COM walk produced: name, type, comment?, address?, initial_value?,
    plus hardware_owner? (I/O-configuration-managed variables)."""
    rows = []
    for m in _VAR.finditer(text):
        name, typ, attrs, body = (m.group(1), m.group(2), m.group(3), m.group(4)) if m.group(1) else \
            (m.group(5), m.group(6), m.group(7), "")
        a = dict(_ATTR.findall(attrs or ""))
        row: dict[str, Any] = {"name": html.unescape(name), "type": html.unescape(typ)}
        own = body.split("<instanceElementDesc", 1)[0]          # the variable's own part, not its members
        cm = re.search(r"<comment>(.*?)</comment>", own, re.S)
        if cm and cm.group(1).strip():
            row["comment"] = html.unescape(cm.group(1).strip())
        if a.get("topologicalAddress"):
            row["address"] = a["topologicalAddress"]
        iv = re.search(r'<variableInit value="([^"]*)"', own)
        if iv and iv.group(1) != "":
            row["initial_value"] = html.unescape(iv.group(1))
        ow = re.search(r'<attribute name="Owner" value="([^"]*)"', own)
        if ow:
            row["hardware_owner"] = html.unescape(ow.group(1))
        rows.append(row)
    return rows


def filter_page(rows: list[dict], name_filter: str = "", type_filter: str = "", offset: int = 0,
                max_results: int = 200) -> dict:
    needle, tf = (name_filter or "").lower(), (type_filter or "").lower()
    hits = [r for r in rows if (not needle or needle in r["name"].lower())
            and (not tf or r["type"].lower() == tf)]
    offset = max(0, int(offset))
    page = hits[offset:offset + max(1, int(max_results))]
    res = {"total_variables": len(rows), "matched": len(hits), "offset": offset, "returned": len(page),
           "variables": page}
    if offset + len(page) < len(hits):
        res["next_offset"] = offset + len(page)
    return res


def list_variables(ce, name_filter: str, max_results: int, offset: int = 0, type_filter: str = "") -> dict:
    from . import constants as C

    proj = ce._project(write=False)
    try:
        rows = parse_variables_export(ce._export_to_text(proj.Variables, ".xsy", C.EXPORT_VAR_ALL))
    except Exception:  # noqa: BLE001 — fall back to the per-variable COM walk
        rows = None
    if not rows:
        res = ce._do_list_variables(name_filter or None, offset + max_results)
        res["variables"] = res.get("variables", [])[offset:]
        res["note"] = "listed by walking variables over COM (export unavailable); slower on big projects"
        return res
    return filter_page(rows, name_filter, type_filter, offset, max_results)
