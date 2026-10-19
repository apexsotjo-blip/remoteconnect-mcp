"""Build Control Expert FBD sections from a block/wire description.

The caller says WHAT to wire; this module works out WHERE: block sizes, pin
coordinates, link endpoints and bend points, page size, the <dataBlock>, and
anonymous-instance numbering. It contains no COM and is unit-testable.

Description format (all names are case-sensitive as in Control Expert):

    blocks = [
        {"name": "Ready",  "type": "AND_BOOL",
         "inputs": {"IN1": "PumpAuto", "IN2": "NOT PumpFault", "IN3": "LevelOk"}},
        {"name": "Delay",  "type": "TON",
         "inputs": {"IN": "Ready.OUT", "PT": "t#3s"},
         "outputs": {"Q": "PumpRun"}, "comment": "3 s start delay"},
    ]

* inputs value: a variable or literal (``PumpAuto``, ``t#3s``, ``12.5``,
  ``TRUE``), a wire ``"<block>.<OUTPUT_PIN>"``, or either prefixed with
  ``NOT `` for an inverted (negated) pin.
* outputs value: a variable to write. An output may also feed wires.
* name: for function-block types (EFB/DFB) this is the instance variable;
  for elementary functions (EF) it is only a reference name — CE numbers
  them ``.1``, ``.2`` … automatically.

Geometry (validated against Control Expert exchange files and builds):
block height = 4 + max(#inputs, #outputs) with EN/ENO hidden; pin row i =
posY + 4 + i; input pins at column posX, output pins at posX + width - 1.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

_Q = {'"': "&quot;"}
WIDTH = 8                      # block width in grid units (CE accepts 7-8 for most blocks)
COL_GAP = 14                   # columns between blocks: room for bound-variable labels + wire channels
ROW_GAP = 2                    # rows between stacked blocks
MAX_COL_ROWS = 80              # wrap a column into sub-columns beyond this height (CE's own sheets stay < ~90 rows)
MAX_X = 110                    # never wrap past this column (CE's own sections stay within ~120 columns)
SAFE_ROWS = 190                # largest drawing height proven to import (IWT AIs, 189 rows); 252 rows failed
LEFT_MARGIN = 12               # room for input variable labels of the first column
TOP_MARGIN = 2

_WIRE = re.compile(r"^(?P<blk>[A-Za-z_][\w]*)\.(?P<pin>[A-Za-z_][\w]*)$")
_IDENT = re.compile(r"^[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*|\[[^\]]+\])*$")
_LITERAL = re.compile(
    r"^(?:TRUE|FALSE|[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|16#[0-9A-Fa-f_]+|8#[0-7_]+|2#[01_]+|"
    r"(?:t|time|T|TIME)#[\w.]+|(?:d|date|dt|tod)#[\w:.\-]+|'[^']*'|\"[^\"]*\"|"
    r"[A-Za-z_]+#[^\s]+)$", re.I)


class FbdError(ValueError):
    """A description problem the caller can fix (unknown pin, bad wire, cycle...)."""


@dataclass
class Pin:
    name: str
    type: str
    direction: str             # "in" | "out" | "inout"


@dataclass
class BlockSpec:
    """Interface of a block type as needed for layout."""
    type: str
    kind: str                  # "EF" (function, anonymous) | "EFB" | "DFB"
    inputs: list[Pin]
    outputs: list[Pin]
    extensible: str | None = None   # input prefix of extensible EFs, e.g. "IN" for AND_BOOL
    min_inputs: int = 2
    width: int | None = None        # Control Expert's own block width; output pins sit at x+width-1,
                                    # so a wrong width misplaces every wire leaving the block


@dataclass
class _Placed:
    name: str
    spec: BlockSpec
    instance: str
    in_pins: list[str]
    out_pins: list[str]
    x: int = 0
    y: int = 0
    col: int = 0
    width: int = WIDTH
    height: int = 0
    inputs: dict = field(default_factory=dict)
    outputs: dict = field(default_factory=dict)
    comment: str = ""

    def in_pos(self, pin: str) -> tuple[int, int]:
        return self.x, self.y + 4 + self.in_pins.index(pin)

    def out_pos(self, pin: str) -> tuple[int, int]:
        return self.x + self.width - 1, self.y + 4 + self.out_pins.index(pin)


def _inverted(value: str) -> tuple[bool, str]:
    """A leading NOT means an inverted pin only when it applies to ONE operand (a variable,
    literal or wire). 'NOT a AND b' is an ST expression bound as-is — inverting the pin would
    compute NOT(a AND b) instead."""
    v = value.strip()
    for pre in ("NOT ", "not ", "Not ", "!"):
        if v.startswith(pre):
            rest = v[len(pre):].strip()
            if _IDENT.match(rest) or _LITERAL.match(rest):
                return True, rest
            return False, v
    return False, v


def classify(value: str, block_names: set[str]) -> str:
    """'wire' | 'literal' | 'variable' | 'expression'.
    Control Expert accepts an ST expression on an FBD pin ('MBTV6.Closed AND NOT WP2.Auto');
    it is bound verbatim and its variables must already exist."""
    m = _WIRE.match(value)
    if m and m.group("blk") in block_names:
        return "wire"
    if _LITERAL.match(value):
        return "literal"
    if _IDENT.match(value):
        return "variable"
    if value.strip():
        return "expression"
    raise FbdError("empty pin value.")


def build_fbd_section(section: str, task: str, blocks: list[dict], specs: dict[str, BlockSpec],
                      known_types: dict[str, str], declare: dict[str, str] | None = None,
                      comment: str = "", product: str = "Control Expert V16.2",
                      max_col_rows: int | None = None) -> dict:
    """Return {"xml", "layout", "declared", "wires", "warnings"}.

    specs: block type -> BlockSpec (from the FFB catalog / project DFBs).
    known_types: existing project variable name -> type (never re-declared).
    declare: extra {variable: type} to declare (overrides inference)."""
    declare = dict(declare or {})
    warnings: list[str] = []
    if not blocks:
        raise FbdError("blocks is empty.")
    names = [b.get("name") or "" for b in blocks]
    if any(not n for n in names):
        raise FbdError("every block needs a 'name' (used as instance name or wire reference).")
    if len(set(names)) != len(names):
        raise FbdError(f"duplicate block names: {sorted({n for n in names if names.count(n) > 1})}")
    block_names = set(names)

    placed: dict[str, _Placed] = {}
    ef_counter = 0
    for b in blocks:
        btype = b.get("type") or ""
        spec = specs.get(btype) or specs.get(btype.upper())
        if spec is None:
            raise FbdError(f"Unknown block type '{btype}' for '{b['name']}'. Use list_function_blocks "
                           "to find the exact type name (DFBs must exist in the project).")
        ins = dict(b.get("inputs") or {})
        outs = dict(b.get("outputs") or {})
        in_pins = [p.name for p in spec.inputs if p.direction in ("in", "inout")]
        if spec.extensible:
            ext = sorted((k for k in ins if re.fullmatch(rf"{spec.extensible}\d+", k)),
                         key=lambda k: int(k[len(spec.extensible):]))
            n = max([spec.min_inputs] + [int(k[len(spec.extensible):]) for k in ext])
            fixed = [p for p in in_pins if not re.fullmatch(rf"{spec.extensible}\d+", p)]
            in_pins = fixed + [f"{spec.extensible}{i}" for i in range(1, n + 1)]
        out_pins = [p.name for p in spec.outputs if p.direction in ("out", "inout")]
        for k in ins:
            if k not in in_pins:
                raise FbdError(f"'{b['name']}' ({btype}) has no input pin '{k}'. Inputs: {in_pins}")
        for k in outs:
            if k not in out_pins:
                raise FbdError(f"'{b['name']}' ({btype}) has no output pin '{k}'. Outputs: {out_pins}")
        if spec.kind == "EF":
            ef_counter += 1
            instance = f".{ef_counter}"
        else:
            if not _IDENT.match(b["name"]) or "." in b["name"]:
                raise FbdError(f"'{b['name']}' is not a valid instance variable name.")
            instance = b["name"]
        placed[b["name"]] = _Placed(b["name"], spec, instance, in_pins, out_pins,
                                    inputs=ins, outputs=outs, comment=str(b.get("comment") or ""),
                                    width=spec.width or WIDTH,
                                    height=4 + max(len(in_pins), len(out_pins), 1))

    # ---- wires and execution order (topological columns)
    wires: list[tuple[str, str, str, str, bool]] = []      # src, src_pin, dst, dst_pin, inverted
    deps: dict[str, set[str]] = {n: set() for n in placed}
    for p in placed.values():
        for pin, raw in p.inputs.items():
            inv, val = _inverted(str(raw))
            if classify(val, block_names) == "wire":
                m = _WIRE.match(val)
                src, spin = m.group("blk"), m.group("pin")
                if spin not in placed[src].out_pins:
                    raise FbdError(f"wire '{val}' -> {p.name}.{pin}: '{src}' has no output '{spin}'. "
                                   f"Outputs: {placed[src].out_pins}")
                if src == p.name:
                    raise FbdError(f"'{p.name}' wires to itself; use a variable for feedback.")
                wires.append((src, spin, p.name, pin, inv))
                deps[p.name].add(src)
    col: dict[str, int] = {}

    def depth(n: str, stack: tuple = ()) -> int:
        if n in col:
            return col[n]
        if n in stack:
            raise FbdError(f"wire loop through {' -> '.join(stack + (n,))}; "
                           "break feedback with a variable.")
        col[n] = 1 + max((depth(d, stack + (n,)) for d in deps[n]), default=-1)
        return col[n]

    for n in placed:
        depth(n)

    # ---- placement: columns left->right, blocks stacked in description order
    next_y: dict[int, int] = {}
    order: list[str] = []
    # A logical column (signal-flow depth) taller than MAX_COL_ROWS wraps into side-by-side sub-columns,
    # so 38 independent blocks become a compact grid instead of a 400-row strip. All sub-columns of
    # depth c lie left of depth c+1, so wires still run left to right.
    # Blocks carrying "pos": [x, y] (e.g. from read_fbd_logic) keep their place, so editing one pin does
    # not redraw the engineer's sheet; only blocks without a position are auto-placed, right of them.
    kept = {}
    for b in blocks:
        pos = b.get("pos")
        if pos is not None:
            if not (isinstance(pos, (list, tuple)) and len(pos) == 2 and all(isinstance(v, int) and v >= 0 for v in pos)):
                raise FbdError(f"'{b['name']}': pos must be [x, y] with non-negative integers, got {pos!r}.")
            kept[b["name"]] = (int(pos[0]), int(pos[1]))
    for n, (px, py) in kept.items():
        p = placed[n]
        p.col, p.x, p.y = col[n], px, py
        order.append(n)
    x = LEFT_MARGIN if not kept else max(placed[n].x + placed[n].width for n in kept) + COL_GAP
    wrap_at = max_col_rows or MAX_COL_ROWS
    for c in sorted(set(col.values())):
        members = [n for n in names if col[n] == c and n not in kept]
        if not members:
            continue
        sub, y = [[]], TOP_MARGIN
        widest = max(placed[n].width for n in members)
        for n in members:
            h = placed[n].height + ROW_GAP
            next_x = x + len(sub) * (widest + COL_GAP)          # where a new sub-column would start
            if sub[-1] and y + h > TOP_MARGIN + wrap_at and next_x + widest <= MAX_X:
                sub.append([])
                y = TOP_MARGIN
            sub[-1].append(n)
            y += h
        for group in sub:
            y = TOP_MARGIN
            for n in group:
                p = placed[n]
                p.col, p.x, p.y = c, x, y
                y += p.height + ROW_GAP
                order.append(n)
            x += max(placed[n].width for n in group) + COL_GAP

    # ---- declarations
    decl: dict[str, str] = {}
    lower_known = {k.lower(): v for k, v in known_types.items()}

    def need(var: str, pin_type: str, where: str):
        base = var.split(".")[0].split("[")[0]
        if base.lower() in lower_known or base in decl:
            return
        if base in declare:
            decl[base] = declare[base]
            return
        if base != var:
            warnings.append(f"{where}: '{var}' is a member of '{base}', which is not declared.")
            return
        t = pin_type.upper()
        if not t or t.startswith("ANY") or t in ("<GENERIC>",):
            raise FbdError(f"{where}: cannot infer a type for new variable '{var}' (pin type {pin_type or '?'}); "
                           f"pass declare={{'{var}': '<TYPE>'}}.")
        decl[base] = pin_type

    def concrete_input_type(p: _Placed) -> str:
        """Type of a generic EF from its bound input variables (ADD of two REALs -> REAL)."""
        for raw in p.inputs.values():
            _, val = _inverted(str(raw))
            if classify(val, block_names) != "variable":
                continue
            base = val.split(".")[0].split("[")[0]
            t = declare.get(base) or decl.get(base) or lower_known.get(base.lower())
            if t and not t.upper().startswith("ANY"):
                return t
        return ""

    for p in placed.values():
        if p.spec.kind != "EF" and p.instance.lower() not in lower_known:
            decl[p.instance] = p.spec.type
        guess = concrete_input_type(p) if p.spec.kind == "EF" else ""
        types_in = {q.name: (guess if q.type.upper().startswith("ANY") and guess else q.type)
                    for q in p.spec.inputs}
        types_out = {q.name: (guess if q.type.upper().startswith("ANY") and guess else q.type)
                     for q in p.spec.outputs}
        for pin, raw in p.inputs.items():
            _, val = _inverted(str(raw))
            if classify(val, block_names) == "variable":
                need(val, types_in.get(pin) or (types_in.get(f"{p.spec.extensible}1") if p.spec.extensible else "")
                     or "", f"{p.name}.{pin}")
        for pin, val in p.outputs.items():
            if classify(str(val), block_names) != "variable":
                raise FbdError(f"{p.name}.{pin}: an output can only be bound to a variable, got '{val}'.")
            need(str(val), types_out.get(pin, ""), f"{p.name}.{pin}")
    for k, v in declare.items():
        if k.lower() not in lower_known:
            decl.setdefault(k, v)

    # ---- XML
    wired_in = {(d, dp) for _, _, d, dp, _ in wires}
    blocks_xml = []
    for n in order:
        p = placed[n]
        pins = ['\t\t\t\t\t\t<inputVariable invertedPin="false" formalParameter="EN"></inputVariable>']
        for pin in p.in_pins:
            inv, val = _inverted(str(p.inputs.get(pin, "")))
            eff = ""
            if pin in p.inputs and (n, pin) not in wired_in:
                eff = f' effectiveParameter="{escape(val, _Q)}"'
            pins.append(f'\t\t\t\t\t\t<inputVariable invertedPin="{"true" if inv else "false"}" '
                        f'formalParameter="{pin}"{eff}></inputVariable>')
        pins.append('\t\t\t\t\t\t<outputVariable invertedPin="false" formalParameter="ENO"></outputVariable>')
        for pin in p.out_pins:
            eff = f' effectiveParameter="{escape(str(p.outputs[pin]), _Q)}"' if pin in p.outputs else ""
            pins.append(f'\t\t\t\t\t\t<outputVariable invertedPin="false" formalParameter="{pin}"{eff}>'
                        '</outputVariable>')
        extra = max(0, len(p.in_pins) - p.spec.min_inputs) if p.spec.extensible else 0
        cmt = f"\n\t\t\t\t\t<comment>{escape(p.comment)}</comment>" if p.comment else ""
        blocks_xml.append(
            f'\t\t\t\t<FFBBlock instanceName="{escape(p.instance, _Q)}" typeName="{escape(p.spec.type, _Q)}" '
            f'additionnalPinNumber="{extra}" enEnO="false" width="{p.width}" height="{p.height}">\n'
            f'\t\t\t\t\t<objPosition posX="{p.x}" posY="{p.y}"></objPosition>\n'
            f'\t\t\t\t\t<descriptionFFB execAfter="">\n' + "\n".join(pins) + "\n"
            f'\t\t\t\t\t</descriptionFFB>{cmt}\n\t\t\t\t</FFBBlock>')

    links_xml, channel = [], {}
    def pt(x, y):
        return f'\t\t\t\t\t<gridObjPosition posX="{max(0, x)}" posY="{max(0, y)}"></gridObjPosition>\n'

    for src, spin, dst, dpin, _ in wires:
        (xs, ys), (xd, yd) = placed[src].out_pos(spin), placed[dst].in_pos(dpin)
        bends = ""
        if src in kept or dst in kept:
            # geometric routing between fixed positions
            k = channel.get((xs, xd), 0)
            channel[(xs, xd)] = k + 1
            if ys == yd and xd > xs:
                bends = ""
            elif xd >= xs + 3:
                xb = min(xs + 2 + k, xd - 2)
                bends = pt(xb, ys) + pt(xb, yd)
            else:                                            # target left of / right next to the source
                ylane = max(placed[src].y + placed[src].height, placed[dst].y + placed[dst].height) + 1 + k
                bends = pt(xs + 1, ys) + pt(xs + 1, ylane) + pt(xd - 2, ylane) + pt(xd - 2, yd)
        elif ys != yd or placed[dst].col != placed[src].col + 1:
            # vertical run in a per-gap channel so parallel wires don't sit on each other
            k = channel.get((placed[src].col, xd), 0)
            channel[(placed[src].col, xd)] = k + 1
            xb = min(xs + 2 + k, xd - 2)
            if placed[dst].col != placed[src].col + 1:          # skip a column: run below the blocks
                ylane = max(q.y + q.height for q in placed.values()) + 1 + k
                bends = (f'\t\t\t\t\t<gridObjPosition posX="{xb}" posY="{ys}"></gridObjPosition>\n'
                         f'\t\t\t\t\t<gridObjPosition posX="{xb}" posY="{ylane}"></gridObjPosition>\n'
                         f'\t\t\t\t\t<gridObjPosition posX="{xd - 2}" posY="{ylane}"></gridObjPosition>\n'
                         f'\t\t\t\t\t<gridObjPosition posX="{xd - 2}" posY="{yd}"></gridObjPosition>\n')
            else:
                bends = (f'\t\t\t\t\t<gridObjPosition posX="{xb}" posY="{ys}"></gridObjPosition>\n'
                         f'\t\t\t\t\t<gridObjPosition posX="{xb}" posY="{yd}"></gridObjPosition>\n')
        links_xml.append(
            "\t\t\t\t<linkFB>\n"
            f'\t\t\t\t\t<linkSource parentObjectName="{escape(placed[src].instance, _Q)}" pinName="{spin}">\n'
            f'\t\t\t\t\t\t<objPosition posX="{xs}" posY="{ys}"></objPosition>\n\t\t\t\t\t</linkSource>\n'
            f'\t\t\t\t\t<linkDestination parentObjectName="{escape(placed[dst].instance, _Q)}" pinName="{dpin}">\n'
            f'\t\t\t\t\t\t<objPosition posX="{xd}" posY="{yd}"></objPosition>\n\t\t\t\t\t</linkDestination>\n'
            f"{bends}\t\t\t\t</linkFB>")

    max_x = max(p.x + p.width for p in placed.values()) + 12
    max_y = max(p.y + p.height for p in placed.values()) + 2 + len(wires)
    # FBDSource nbRows/nbColumns: Control Expert writes 24/36 for its own sections, whose drawings stay
    # within ~90 rows x ~120 columns (the default sheet). A drawing beyond that needs larger values,
    # and they must leave room: blocks are drawn with Control Expert's own width (FC_FSD is 12, not 8),
    # so a tight value rejects edge blocks with E1189 'Object creation error (invalid position)'.
    # nbRows/nbColumns: always Control Expert's own 24/36. Bisected on IWT AIs: the ORIGINAL section XML with
    # this builder's positions imports clean at 24/36 (drawing 189 x 114), while the same blocks with any
    # other nbRows/nbColumns fail with E1189 'Object creation error (invalid position)'. What limits a
    # section is the drawing itself: one 252-row column failed even at 24/36, hence column wrapping.
    nb_cols, nb_rows = 36, 24
    if max_y > SAFE_ROWS or max_x > MAX_X + 20:
        warnings.append(f"large drawing ({max_y} rows x {max_x} columns): Control Expert may reject block "
                        f"positions beyond ~{SAFE_ROWS} rows; consider splitting this section.")
    text = ""
    if comment:
        text = (f'\t\t\t\t<textBox width="{min(max_x - 2, max(12, len(comment) // 2))}" height="2">'
                f'{escape(comment)}<objPosition posX="1" posY="0"></objPosition></textBox>\n')
    datablock = "".join(f'\t\t<variables name="{escape(k, _Q)}" typeName="{escape(v, _Q)}"></variables>\n'
                        for k, v in decl.items())
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<FBDExchangeFile>\n'
        f'\t<fileHeader company="Schneider Automation" product="{escape(product, _Q)}" '
        'content="Function Block Diagram source file" DTDVersion="41"></fileHeader>\n'
        '\t<contentHeader name="Project" version="0.0.000"></contentHeader>\n\t<program>\n'
        f'\t\t<identProgram name="{escape(section, _Q)}" type="section" task="{escape(task, _Q)}"></identProgram>\n'
        f'\t\t<FBDSource nbRows="{nb_rows}" nbColumns="{nb_cols}">\n\t\t\t<networkFBD>\n'
        + text + "\n".join(blocks_xml + links_xml) +
        "\n\t\t\t</networkFBD>\n\t\t</FBDSource>\n\t</program>\n"
        + (f"\t<dataBlock>\n{datablock}\t</dataBlock>\n" if datablock else "")
        + "</FBDExchangeFile>\n")
    layout = [{"name": n, "type": placed[n].spec.type, "instance": placed[n].instance,
               "column": placed[n].col, "x": placed[n].x, "y": placed[n].y,
               "width": placed[n].width, "height": placed[n].height} for n in order]
    return {"xml": xml, "layout": layout, "declared": decl,
            "wires": [f"{s}.{sp} -> {d}.{dp}" + (" (inverted)" if i else "") for s, sp, d, dp, i in wires],
            "warnings": warnings, "extent": {"rows": max_y, "columns": max_x}}


def block_at(layout: list[dict], col: int, line: int) -> str | None:
    """Map a build-message grid position (c, l) back to the block drawn there."""
    for b in layout:
        if b["x"] <= col < b["x"] + b["width"] + 1 and b["y"] <= line < b["y"] + b["height"] + 1:
            return b["name"]
    return None
