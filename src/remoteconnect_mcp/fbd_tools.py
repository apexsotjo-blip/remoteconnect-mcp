"""Bridge-side FBD tools: function-block catalog, live interface lookup, write/read FBD sections.

Functions here run on the bridge's COM worker thread (call them through bridge._run) and use
only the bridge's existing primitives, so the same file works in control-expert-mcp and
remoteconnect-mcp."""
from __future__ import annotations

import re
import uuid
from typing import Any

from . import ffb_catalog
from .fbd_builder import FbdError, block_at, build_fbd_section

_MSG = re.compile(r"\{(?P<sec>[^:}]+?)\s*:\s*\[(?P<task>[^\]]*)\]\}\s*:\s*(?:\(l:\s*(?P<l>\d+),\s*c:\s*(?P<c>\d+)\)\s*)?"
                  r"(?P<code>[EW]\d{4})\s*(?P<text>.*)")


# ------------------------------------------------------------------ interfaces
_VAR = re.compile(r'<variables name="([^"]+)" typeName="([^"]+)"[^>]*>(.*?)</variables>', re.S)
_POS = re.compile(r'<attribute name="PositionPin" value="(\d+)"')


def dfb_entry_from_xml(name: str, xml: str) -> dict:
    """DFB interface in Control Expert's pin order. The block's input side lists inputs AND
    in/out parameters interleaved by their PositionPin attribute (IWT FC_FSD has its in/out
    'FSD' at position 5 of 13); importing pins in any other order is rejected."""
    def params(section: str, direction: str) -> list[tuple[int, int, dict]]:
        m = re.search(rf"<{section}>(.*?)</{section}>", xml, re.S)
        rows = []
        for i, (n, t, body) in enumerate(_VAR.findall(m.group(1)) if m else []):
            p = _POS.search(body)
            rows.append((int(p.group(1)) if p else 10_000 + i, i, {"name": n, "type": t, "dir": direction}))
        return rows

    in_side = sorted(params("inputParameters", "in") + params("inOutParameters", "inout"), key=lambda r: r[:2])
    outs = sorted(params("outputParameters", "out"), key=lambda r: r[:2])
    cm = re.search(r"<FBSource[^>]*>\s*<comment>(.*?)</comment>", xml, re.S)
    return {"name": name, "kind": "DFB", "family": "Project DFB",
            "description": (cm.group(1).strip() if cm else ""),
            "pins": [r[2] for r in in_side] + [r[2] for r in outs]}


def _project_dfbs(ce, only: set[str] | None = None) -> list[dict]:
    import win32com.client
    from . import constants as C
    from .bridge import _iter_collection

    proj = ce._project(write=False)
    out = []
    for it in _iter_collection(proj.Dfbs):
        item = win32com.client.Dispatch(it)
        name = str(item.Name)
        if only is not None and name.upper() not in only:
            continue
        try:
            out.append(dfb_entry_from_xml(name, ce._export_to_text(item, ".xdb", C.EXPORT_BASIC)))
        except Exception:  # noqa: BLE001
            try:
                ins, outs, inouts = ce._fb_interface(proj, name)
            except Exception:  # noqa: BLE001
                ins, outs, inouts = [], [], []
            out.append(ffb_catalog.dfb_entry(name, ins, outs, inouts))
    return out


def _dfb_names(ce) -> list[str]:
    import win32com.client
    from .bridge import _iter_collection

    try:
        proj = ce._project(write=False)
        return [str(win32com.client.Dispatch(it).Name) for it in _iter_collection(proj.Dfbs)]
    except Exception:  # noqa: BLE001
        return []


def _live_efb(ce, type_name: str) -> dict | None:
    """Read an EFB interface from the open project's libset: declare a temporary instance,
    read IEfb.ListSubfields, delete the instance. Returns None for functions (EF) or unknown."""
    import win32com.client
    from .bridge import _iter_collection

    tmp = f"zz_mcp_probe_{uuid.uuid4().hex[:8]}"
    try:
        ce._do_create_variable(tmp, type_name, None, None, None)
    except Exception:  # noqa: BLE001 — not instantiable: a function, or not in this libset
        return None
    try:
        proj = ce._project(write=True)
        for it in _iter_collection(proj.Efbs):
            e = win32com.client.Dispatch(it)
            if str(e.Name).upper() != type_name.upper():
                continue
            pn, pt, _, pk = e.ListSubfields()
            pins = [{"name": str(n), "type": str(t), "dir": ffb_catalog.NATURE[str(k)]}
                    for n, t, k in zip(pn, pt, pk) if str(k) in ffb_catalog.NATURE]
            return {"name": str(e.Name), "kind": "EFB", "family": "live (open project libset)",
                    "description": "", "pins": pins}
        return None
    finally:
        try:
            ce._do_delete_variable(tmp)
        except Exception:  # noqa: BLE001
            pass


def resolve_entry(ce, type_name: str) -> dict | None:
    """Catalog -> project DFB -> live libset lookup."""
    e = ffb_catalog.find(type_name)
    if e and e.get("pins") and not e.get("pins_unknown"):
        return e
    if type_name.upper() in {n.upper() for n in _dfb_names(ce)}:
        dfbs = _project_dfbs(ce, {type_name.upper()})
        if dfbs:
            return dfbs[0]
    if ce._app is not None:
        live = _live_efb(ce, type_name)
        if live:
            return live
    return e


_NAME = re.compile(r"^(?:NOT\s+|!)?([A-Za-z_]\w*)")


def _referenced_names(blocks: list[dict], declare: dict | None) -> set[str]:
    """Base identifiers a diagram may declare: instance names, bound variables, declare keys."""
    names = {b.get("name", "") for b in blocks} | set(declare or {})
    for b in blocks:
        for v in list((b.get("inputs") or {}).values()) + list((b.get("outputs") or {}).values()):
            m = _NAME.match(str(v).strip())
            if m:
                names.add(m.group(1))
    return {n for n in names if n}


def _existing_types(ce, names: set[str]) -> dict[str, str]:
    """Types of the given names that already exist as project variables. Looks each name up
    directly (Variables.Item) instead of walking every variable — IWT-size projects have
    thousands, and a full COM walk per write took minutes. Falls back to the walk."""
    proj = ce._project(write=False)
    out: dict[str, str] = {}
    try:
        coll = proj.Variables
        for n in names:
            try:
                v = coll.Item(n)
            except Exception:  # noqa: BLE001 — not a variable (literal, wire label, new name)
                continue
            try:
                out[str(v.Name)] = str(v.TypeName)
            except Exception:  # noqa: BLE001
                pass
        return out
    except Exception:  # noqa: BLE001
        from .bridge import _iter_collection
        wanted = {n.lower() for n in names}
        for v in _iter_collection(proj.Variables):
            try:
                if str(v.Name).lower() in wanted:
                    out[str(v.Name)] = str(v.TypeName)
            except Exception:  # noqa: BLE001
                pass
        return out


# ------------------------------------------------------------------ public ops
def list_function_blocks(ce, query: str, family: str, kind: str, limit: int) -> dict:
    dfbs = []
    if ce._app is not None and kind.upper() in ("", "DFB"):
        names = _dfb_names(ce)
        q = query.lower()
        wanted = {n.upper() for n in names if not q or q in n.lower()}
        dfbs = _project_dfbs(ce, wanted) if len(wanted) <= 60 else \
            [ffb_catalog.dfb_entry(n, [], [], []) for n in names if n.upper() in wanted]
    return ffb_catalog.search(query, family, kind, dfbs, limit)


def describe_function_block(ce, type_name: str) -> dict:
    e = resolve_entry(ce, type_name)
    if not e:
        similar = [r["name"] for r in ffb_catalog.search(type_name[:4], limit=15)["blocks"]]
        raise FbdError(f"'{type_name}' is not a known function or function block. Similar: {similar}")
    ins = [p for p in e["pins"] if p["dir"] in ("in", "inout")]
    outs = [p for p in e["pins"] if p["dir"] in ("out", "inout")]
    res: dict[str, Any] = {
        "name": e["name"], "kind": e["kind"], "family": e.get("family", ""),
        "description": e.get("description", ""),
        "inputs": [{"pin": p["name"], "type": p["type"]} for p in ins],
        "outputs": [{"pin": p["name"], "type": p["type"]} for p in outs],
        "instance": ("anonymous (functions get .1, .2 ... automatically)" if e["kind"] == "EF"
                     else "named: the block 'name' becomes an instance variable of this type"),
    }
    if e.get("extensible"):
        res["extensible"] = (f"add {e['extensible']}3, {e['extensible']}4 ... for more inputs "
                             f"(minimum {e.get('min_inputs', 2)})")
    if e.get("pins_unknown"):
        res["warning"] = ("pins are not known for this function in this catalog; write it in an ST "
                          "section instead, or check the Control Expert help for its pin names.")
    res["fbd_example"] = {"name": "B1", "type": e["name"],
                          "inputs": {p["name"]: f"<var|literal|Block.PIN>" for p in ins[:3]},
                          "outputs": {p["name"]: "<var>" for p in outs[:2]}}
    return res


_WIDTH_RE = re.compile(r'<FFBBlock instanceName="([^"]*)" typeName="([^"]+)" additionnalPinNumber="\d+" '
                       r'enEnO="\w+" width="(\d+)"')


def _width_cache(ce) -> dict[str, int]:
    if not hasattr(ce, "_fbd_widths"):
        ce._fbd_widths = {}
    return ce._fbd_widths


def remember_widths(ce, xml: str) -> None:
    """Every FBD section Control Expert exports carries its true block widths — keep them."""
    cache = _width_cache(ce)
    for _, t, w in _WIDTH_RE.findall(xml):
        cache.setdefault(t.upper(), int(w))


def calibrate_widths(ce, task: str, specs: dict) -> dict[str, int]:
    """Control Expert draws each block type at its own width (OR 7, AND_BOOL 8, FC_FSD 12 ...) whatever
    the XML says, and wrapped columns need the real width to avoid overlapping blocks. Learn unknown
    widths once per session: import one throw-away section stacking one block of each type in a single
    unwrapped column, export it, read the widths, delete the section and its temporary instances."""
    cache = _width_cache(ce)
    todo = [s for t, s in specs.items() if t.upper() not in cache]
    if not todo or ce._app is None:
        return cache
    name = f"ZZ_MCP_W{uuid.uuid4().hex[:6].upper()}"
    blocks = [{"name": f"zzw_{uuid.uuid4().hex[:6]}_{i}", "type": s.type} for i, s in enumerate(todo)]
    sec = build_fbd_section(name, task, blocks, {s.type: s for s in todo}, {}, max_col_rows=100_000)
    temp_vars = [n for n, t in sec["declared"].items() if n.startswith("zzw_")]
    try:
        ce._do_import_xml(sec["xml"], None, "section", task, "overwrite")
        remember_widths(ce, ce._do_read_section(task, name)["xml"])
    except Exception:  # noqa: BLE001 — calibration is best effort; the default width is used instead
        pass
    finally:
        try:
            ce._do_delete_section(task, name)
        except Exception:  # noqa: BLE001
            pass
        for v in temp_vars:
            try:
                ce._do_delete_variable(v)
            except Exception:  # noqa: BLE001
                pass
    return cache


def write_fbd_logic(ce, task: str, section: str, blocks: list[dict], declare: dict[str, str] | None,
                    comment: str, build: bool, dry_run: bool) -> dict:
    types = {b.get("type", "") for b in blocks}
    specs, unknown = {}, []
    for t in types:
        e = resolve_entry(ce, t) if ce._app is not None else ffb_catalog.find(t)
        if not e or not e.get("pins") or e.get("pins_unknown"):
            unknown.append(t)
            continue
        specs[t] = ffb_catalog.to_spec(e)
    if unknown:
        raise FbdError(f"no pin interface for {sorted(unknown)}. Check the type with list_function_blocks; "
                       "DFBs must exist in the project; functions without known pins belong in ST.")
    known = _existing_types(ce, _referenced_names(blocks, declare)) if ce._app is not None else {}
    widths = calibrate_widths(ce, task, specs) if ce._app is not None and not dry_run else _width_cache(ce)
    for t, s in specs.items():
        s.width = widths.get(t.upper(), s.width)
    sec = build_fbd_section(section, task, blocks, specs, known, declare, comment)
    result: dict[str, Any] = {"section": section, "task": task, "layout": sec["layout"],
                              "wires": sec["wires"], "declared": sec["declared"], "extent": sec["extent"]}
    if sec["warnings"]:
        result["warnings"] = sec["warnings"]
    if dry_run:
        result["xml"] = sec["xml"]
        return result
    imp = ce._do_import_xml(sec["xml"], None, "section", task, "overwrite")
    result["imported"] = imp.get("imported", True)
    if build:
        b = ce._do_build(False)
        msgs = []
        for ln in (b.get("output") or "").splitlines():
            m = _MSG.search(ln)
            if not m or m.group("sec").strip().lower() != section.lower():
                continue
            entry = {"code": m.group("code"), "message": m.group("text").strip()[:200]}
            if m.group("l"):
                entry["at"] = {"line": int(m.group("l")), "column": int(m.group("c"))}
                hit = block_at(sec["layout"], int(m.group("c")), int(m.group("l")))
                if hit:
                    entry["block"] = hit
            msgs.append(entry)
        result["build_state"] = b.get("build_state")
        result["section_messages"] = msgs
        others = [ln for ln in (b.get("output") or "").splitlines()
                  if re.search(r"\b[E]\d{4}\b", ln) and section.lower() not in ln.lower()]
        if others:
            result["other_sections_errors"] = len(others)
    return result


def read_fbd_logic(ce, task: str, section: str) -> dict:
    """Turn an existing FBD section back into the write_fbd_logic description (positions dropped)."""
    data = ce._do_read_section(task, section)
    if data.get("language") != "FBD":
        raise FbdError(f"section '{section}' is {data.get('language')}, not FBD.")
    xml = data["xml"]
    remember_widths(ce, xml)
    blocks, by_inst = [], {}
    counters: dict[str, int] = {}
    for m in re.finditer(r'<FFBBlock instanceName="([^"]*)" typeName="([^"]+)"[^>]*>(.*?)</FFBBlock>', xml, re.S):
        inst, typ, body = m.group(1), m.group(2), m.group(3)
        if inst.startswith("."):
            counters[typ] = counters.get(typ, 0) + 1
            name = f"{typ}_{counters[typ]}"
        else:
            name = inst
        b: dict[str, Any] = {"name": name, "type": typ, "inputs": {}, "outputs": {}}
        pm = re.search(r'<objPosition posX="(\d+)" posY="(\d+)"', body)
        if pm:
            b["pos"] = [int(pm.group(1)), int(pm.group(2))]
        for io, inv, pin, eff in re.findall(
                r'<(input|output)Variable invertedPin="(true|false)" formalParameter="([^"]+)"'
                r'(?: effectiveParameter="([^"]*)")?', body):
            if pin in ("EN", "ENO"):
                continue
            if io == "input" and (eff or inv == "true"):
                simple = re.fullmatch(r"[A-Za-z_][\w.\[\]]*|[\w#.+\-']+", eff or "")
                # an inverted pin carrying an expression must stay NOT(<expr>) when re-read
                b["inputs"][pin] = (("NOT " if simple or not eff else "NOT (") if inv == "true" else "") \
                    + (eff or "") + (")" if inv == "true" and eff and not simple else "")
            elif io == "output" and eff:
                b["outputs"][pin] = eff
        cm = re.search(r"<comment>(.*?)</comment>", body, re.S)
        if cm:
            b["comment"] = cm.group(1).strip()
        blocks.append(b)
        by_inst[inst] = b
    for m in re.finditer(r'<linkSource parentObjectName="([^"]+)" pinName="([^"]+)">.*?'
                         r'<linkDestination parentObjectName="([^"]+)" pinName="([^"]+)">', xml, re.S):
        src, spin, dst, dpin = m.groups()
        if src in by_inst and dst in by_inst:
            prev = by_inst[dst]["inputs"].get(dpin, "")
            inv = "NOT " if prev.startswith("NOT") else ""
            by_inst[dst]["inputs"][dpin] = f"{inv}{by_inst[src]['name']}.{spin}"
    for b in blocks:
        if not b["outputs"]:
            del b["outputs"]
    return {"task": task, "section": section, "blocks": blocks,
            "note": "Edit this description and pass it to write_fbd_logic to rewrite the section. "
                    "Blocks keep their drawing position ('pos'); new blocks without 'pos' are placed "
                    "automatically to the right. Remove every 'pos' to redraw the whole sheet."}
