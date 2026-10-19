"""Generate src/remoteconnect_mcp/ffb_catalog.json from the INSTALLED Control Expert libset.

How: block names come from the libset folder layout (library/family/<NAME>_<target>.OBJ);
each is declared as a variable in a throw-away project, and UDE's IEfb.ListSubfields returns
its exact pins (name, type, direction). Names that cannot be declared are elementary
functions (no instance) and are kept as kind "EF" with unknown pins.
--verify-efs additionally imports and builds one FBD section per built-in elementary
function definition (ffb_catalog._elementary) to prove its pin names.

Nothing is saved. Usage (from the repo, in the server's venv):
  python tools/build_ffb_catalog.py [--platform SCADAPack57x] [--verify-efs] [--libset DIR]
Standard (non-SCADAPack) Control Expert:  --platform std --cpu "BME P58 2040" --cpu-version 03.10
"""
import argparse, datetime, json, os, re, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from remoteconnect_mcp import ffb_catalog                                   # noqa: E402
from remoteconnect_mcp.bridge import ControlExpertBridge, _iter_collection  # noqa: E402
from remoteconnect_mcp.fbd_builder import build_fbd_section                 # noqa: E402

XPDF = r"C:\ProgramData\Schneider Electric\CommonControlExpert\16.2\Xpdf\SCBForSCADAPack.xpdf"
LIBSET = r"C:\Program Files (x86)\Schneider Electric\EcoStruxure Control Expert 16.2\FFBLibset\v16.2"
SUFFIX = re.compile(r"^(?P<name>.+?)_(?:32|ARM|ARM5|ARM7|X86|64)$", re.I)
LANGS = {"ENG", "FRE", "GER", "ITA", "SPA", "CHI"}
CONCRETE = {"ANY_NUM": "REAL", "ANY_BIT": "WORD", "ANY_INT": "INT", "ANY_ELEM": "REAL", "ANY": "INT"}


def scan_libset(root):
    found = {}
    for dirpath, _, files in os.walk(root):
        rel = os.path.relpath(dirpath, root).split(os.sep)
        if len(rel) < 2 or rel[-1] in LANGS or rel[0] in ("ObsoleteLib",):
            continue
        for f in files:
            stem, ext = os.path.splitext(f)
            if ext.upper() in (".OBJ", ".O"):
                m = SUFFIX.match(stem)
                found.setdefault((m.group("name") if m else stem).upper(), (rel[0], "/".join(rel[1:])))
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", default="SCADAPack57x")
    ap.add_argument("--cpu", default="")
    ap.add_argument("--cpu-version", default="")
    ap.add_argument("--libset", default=LIBSET)
    ap.add_argument("--verify-efs", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    names = scan_libset(a.libset)
    if a.limit:
        names = dict(list(names.items())[:a.limit])
    ce = ControlExpertBridge()
    t0 = time.time()
    if a.platform == "std":
        ce.new_project(a.cpu, a.cpu_version)
    else:
        ce.new_logic_editor_project(a.platform, XPDF, None)
    print(f"scratch project ready in {time.time() - t0:.0f}s; declaring {len(names)} candidate blocks")

    declared, not_instantiable = {}, []
    for i, (n, fam) in enumerate(sorted(names.items())):
        try:
            ce.create_variable(f"zcat{i}", n, None, None, None)
            declared[n] = fam
        except Exception:  # noqa: BLE001 — functions (EF) and platform-unsupported blocks
            not_instantiable.append(n)
        if i % 100 == 0:
            print(f"  {i}/{len(names)} ...", flush=True)

    def read_efbs():
        import win32com.client
        proj = ce._project(write=True)
        rows = {}
        for it in _iter_collection(proj.Efbs):
            e = win32com.client.Dispatch(it)
            nm = str(e.Name)
            try:
                pn, pt, _, pk = e.ListSubfields()
            except Exception:  # noqa: BLE001
                continue
            pins = [{"name": str(x), "type": str(t), "dir": ffb_catalog.NATURE[str(k)]}
                    for x, t, k in zip(pn, pt, pk) if str(k) in ffb_catalog.NATURE]
            try:
                comment = str(e.Comment() if callable(e.Comment) else e.Comment)
            except Exception:  # noqa: BLE001
                comment = ""
            rows[nm.upper()] = {"name": nm, "pins": pins, "description": comment.strip()}
        return rows

    efb = ce._run(read_efbs)
    blocks = []
    for n, (lib, fam) in sorted(declared.items()):
        e = efb.get(n)
        if e:
            blocks.append({"name": e["name"], "kind": "EFB", "library": lib, "family": f"{lib}/{fam}",
                           "description": e["description"], "pins": e["pins"]})
    for n in sorted(not_instantiable):
        lib, fam = names[n]
        blocks.append({"name": n, "kind": "EF", "library": lib, "family": f"{lib}/{fam}",
                       "description": "", "pins": [], "pins_unknown": True})

    verified = {}
    if a.verify_efs:
        verified = verify_efs(ce)
        for b in blocks:                                     # a verified definition replaces the unknown stub
            if b["name"] in verified and b.get("pins_unknown"):
                b["pins_unknown"] = False
                b["superseded_by_builtin"] = True
    out = {"source": f"UDE IEfb.ListSubfields on {a.platform}; libset {a.libset}",
           "generated": datetime.datetime.now().isoformat(timespec="seconds"),
           "platform": a.platform, "efb_count": sum(b["kind"] == "EFB" for b in blocks),
           "ef_without_pins": sum(bool(b.get("pins_unknown")) for b in blocks),
           "elementary_verified": verified, "blocks": blocks}
    path = ffb_catalog.CATALOG_FILE
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    print(f"wrote {path}: {out['efb_count']} EFBs with pins, {out['ef_without_pins']} EFs without pins, "
          f"{sum(verified.values())}/{len(verified)} built-in EFs verified")
    ce.close_project(False)
    ce.shutdown()


def verify_efs(ce):
    """One FBD section per built-in EF, wired to fresh typed variables; full build; per-section errors."""
    efs = ffb_catalog._elementary()
    known = {}
    for i, e in enumerate(efs):
        spec = ffb_catalog.to_spec(e)
        ins = {p.name: f"v{i}_{p.name}" for p in spec.inputs}
        outs = {p.name: f"v{i}_{p.name}o" for p in spec.outputs}
        declare = {}
        for p in spec.inputs + spec.outputs:
            t = CONCRETE.get(p.type.upper(), p.type)
            if e["name"] in ("SEL",) and p.name == "G":
                t = "BOOL"
            var = ins.get(p.name) if p in spec.inputs else outs.get(p.name)
            declare[var] = t if not (e["name"].startswith(("GT", "GE", "EQ", "NE", "LE", "LT"))
                                     and p.name == "OUT") else "BOOL"
        sec = build_fbd_section(f"EF_{i:03d}", "MAST",
                                [{"name": "B", "type": e["name"], "inputs": ins, "outputs": outs}],
                                {e["name"]: spec}, known, declare=declare)
        try:
            ce.import_xml(sec["xml"], None, "section", "MAST", "overwrite")
        except Exception as ex:  # noqa: BLE001
            known[f"EF_{i:03d}"] = f"import: {ex}"
    b = ce.build_project(True)
    out = b.get("output", "")
    result = {}
    for i, e in enumerate(efs):
        sec = f"EF_{i:03d}"
        bad = known.get(sec) or any(f"{{{sec} " in ln and re.search(r"\bE\d{4}\b", ln) for ln in out.splitlines())
        result[e["name"]] = not bad
    fails = [k for k, v in result.items() if not v]
    print(f"EF verification: {len(result) - len(fails)}/{len(result)} ok; failing: {fails}")
    if fails:
        for ln in out.splitlines():
            if re.search(r"\bE\d{4}\b", ln):
                print("   ", ln[:220])
    return result


if __name__ == "__main__":
    main()
