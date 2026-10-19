"""MCP server exposing EcoStruxure Control Expert to AI agents.

Transport: stdio. All logs go to stderr (stdout carries JSON-RPC).
Online (live PLC) tools are disabled unless CE_MCP_ENABLE_ONLINE=1.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.etree import ElementTree

from mcp.server.fastmcp import FastMCP

from .bridge import CEError, ControlExpertBridge
from .audit import (
    audit_remoteconnect_bundle,
    configuration_logic_variable_specs,
    merge_configuration_logic_variables,
)
from .modbus import ModbusClient, ModbusError, parse_address
from .prompts import register_prompts
from .fbd_server import register_fbd_tools
from . import schema as ce_schema
from .remoteconnect import (
    discover_bundle,
    inspect_project,
    inventory_project,
    read_feedback,
    read_import_report,
)
from .bulk_edit import (
    BulkEditError,
    create_objects_copy,
    create_parent_entities_copy,
    create_workbook_copy,
    inspect_relationships,
    inspect_workbook,
)
from .native_bridge import (
    NativeBridgeError,
    create_configuration_project,
    export_configuration,
    import_configuration_copy,
)

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("remoteconnect-mcp")

mcp = FastMCP(
    "RemoteConnect",
    instructions=(
        "Programmatically controls local Schneider Electric RemoteConnect FDT/DTMs "
        "and its UnitySoControl logic editor; no RemoteConnect UI automation is used. "
        "For a new complete application call create_remoteconnect_project. For x70 "
        "configuration: export_remoteconnect_configuration -> inspect/author a typed "
        "workbook copy -> apply_remoteconnect_configuration_copy; accept it only when "
        "ok=true. The default strict import withholds the output PRJ on any DTM error.\n\n"
        "Logic uses the EcoStruxure Control Expert UDE automation interface. "
        "Typical flow: open_project (or new_project) -> inspect with "
        "get_project_structure / list_variables / read_section -> edit -> "
        "build_project (its 'output' lists per-section errors; iterate until "
        "'Process succeeded') -> save_project. Writing logic: for ST use "
        "write_st_logic (plain IEC text, no XML); read ST back with read_st_logic "
        "(line-numbered, paged, searchable - not the full XML). For FBD use write_fbd_logic: give "
        "blocks + wires ('Ready.OUT'), it computes all geometry, declares variables, "
        "imports and builds; read_fbd_logic returns an existing FBD section in the "
        "same form for editing. Find blocks and their exact pins with "
        "list_function_blocks / describe_function_block. For LD/SFC, FIRST call "
        "get_language_reference(language) and mirror its validated example via "
        "import_xml(kind='section'). SFC is the right choice for stepwise "
        "sequences (steps + transitions + ST actions).\n\n"
        "Live testing: online tools (plc_*, modbus_*) need CE_MCP_ENABLE_ONLINE=1. "
        "To run a project on the simulator, see the commission_simulator prompt (a "
        "fresh sim needs a one-time manual transfer from the CE GUI to seed the CPU "
        "family — 'Family check failed' otherwise). Reading/writing LIVE values is "
        "done over Modbus TCP (modbus_connect/read_tags/write_tags), not the COM API; "
        "only LOCATED %M/%MW tags are reachable, so mirror unlocated DFB internals to "
        "%MW first (see the test_logic_live prompt). Invoke a guided prompt instead of "
        "rediscovering these flows."
    ),
)

# FastMCP doesn't forward a version to the low-level server, so its serverInfo
# would otherwise report the mcp SDK version; set the real package version.
mcp._mcp_server.version = "0.20.0"

ce = ControlExpertBridge()
mb = ModbusClient()
register_prompts(mcp)
register_fbd_tools(mcp, ce)

ONLINE_ENABLED = os.environ.get("CE_MCP_ENABLE_ONLINE", "").strip() in ("1", "true", "yes")


def _default_type_for(address: str) -> str:
    """Default IEC type when a raw %-address is given without one."""
    try:
        family, _, _ = parse_address(address)
    except ModbusError:
        return "INT"
    return "BOOL" if family in ("M", "MX") else "INT"


def _build_tag_plan(specs: list[str]) -> list[list]:
    """Turn tag specs into [label, address, type] rows. Each spec is a global
    variable name (address+type resolved from the project) or an explicit
    address with optional ':TYPE' (e.g. '%MW2:REAL', '%MW70:UDINT', '%M3')."""
    plan: list[list] = []
    names: list[str] = []
    for spec in specs:
        spec = spec.strip()
        if not spec:
            continue
        if spec.startswith("%"):
            addr, _, typ = spec.partition(":")
            plan.append([spec, addr.strip(), typ.strip() or _default_type_for(addr.strip())])
        else:
            names.append(spec)
            plan.append([spec, None, None])
    if names:
        resolved = ce.resolve_addresses(names)
        for row in plan:
            if row[1] is None:
                rec = resolved.get(row[0], {})
                row[1], row[2] = rec.get("address", ""), rec.get("type", "")
    return plan


# --------------------------------------------------------------- session


@mcp.tool()
def get_status() -> dict:
    """Get the current session status: Control Expert server version, whether a
    project is open, its file path, CPU, version and build state."""
    return ce.get_status()


@mcp.tool()
def open_project(path: str) -> dict:
    """Open a Control Expert project file.

    Supports .stu (native project), .sta (archive), and .xef/.zef (XML exchange
    format, imported into a fresh application). Any previously open project is
    closed without saving. Opening can take 30-120 s for large projects.
    """
    return ce.open_project(path)


@mcp.tool()
def inspect_remoteconnect_project(path: str) -> dict:
    """Inspect a RemoteConnect project bundle without opening or modifying it.

    path may identify a .PRJ, .RCZ, .STU, or .STA file. Returns the matching
    sibling files and a safe summary of the .PRJ FDT/DTM structure. Opaque DTM
    EngineeringData is counted but never deserialized or changed.
    """
    try:
        return inspect_project(path)
    except (FileNotFoundError, ValueError, ElementTree.ParseError) as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool()
def audit_remoteconnect_project(path: str, rebuild_logic: bool = True) -> dict:
    """Audit consistency between a paired RemoteConnect PRJ and STU/STA.

    Headlessly exports the native x70 configuration, validates table relationships
    and known live-editor naming rules, compares every logic-backed object name/DDT with
    the paired logic variables, locates section references, and optionally performs
    a full unsaved rebuild. An isolated logic-editor session is used, both source
    files remain unchanged, and no UI automation is involved.
    """
    try:
        return audit_remoteconnect_bundle(path, rebuild_logic=rebuild_logic)
    except (
        CEError,
        NativeBridgeError,
        BulkEditError,
        OSError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool()
def inventory_remoteconnect_configuration(path: str) -> dict:
    """Read the RemoteConnect FDT/DTM topology and configuration-object index.

    Returns every DTM identifier and its InstanceDataRecord groups. Values in
    those records are proprietary serialized payloads and remain opaque; this
    tool never performs unsafe .NET deserialization or modifies the project.
    """
    try:
        return inventory_project(path)
    except (FileNotFoundError, ValueError, ElementTree.ParseError) as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool()
def read_remoteconnect_feedback(
    log_path: str = "", latest: bool = True, max_files: int = 20
) -> dict:
    """Read structured RemoteConnect Object Bulk Edit feedback.

    With no log_path, reads the newest RemoteConnect bulk-edit log from the
    current Windows user's application-data directory. Set latest=false to
    aggregate up to max_files. Returns per-object changes, warnings and errors,
    source file/line references, counts, and ok=false when errors are present.
    """
    try:
        return read_feedback(log_path or None, latest=latest, max_files=max_files)
    except (FileNotFoundError, ValueError, OSError) as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool()
def inspect_remoteconnect_bulk_edit_workbook(
    path: str, sheet_name: str = "", limit: int = 100
) -> dict:
    """Read a native RemoteConnect Object Bulk Edit `.xls` workbook.

    Returns workbook version/DTM metadata and every sheet's stable machine
    fields. Pass sheet_name to also return up to limit data rows. Use the row-2
    machine fields, not translated row-1 labels, when creating patches.
    """
    try:
        return inspect_workbook(path, sheet_name or None, limit)
    except BulkEditError as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool()
def inspect_remoteconnect_configuration_relationships(
    path: str, max_issues: int = 200
) -> dict:
    """Validate primary keys and every known cross-sheet XLS relationship.

    Checks object, physical-I/O, DNP3, Modbus device/scanner, and Browser List
    foreign keys, uniqueness, and object-name/sequence-ID agreement before the
    workbook reaches the native DTM importer.
    """
    try:
        return inspect_relationships(path, max_issues=max_issues)
    except BulkEditError as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool()
def create_remoteconnect_bulk_edit_copy(
    template_path: str,
    output_path: str,
    patches: list[dict] | None = None,
    additions: list[dict] | None = None,
    project_settings: dict[str, str | int | float | bool] | None = None,
    overwrite: bool = False,
) -> dict:
    """Create a patched copy of a RemoteConnect Object Bulk Edit workbook.

    Each patch is {sheet, match, values}; match must select exactly one existing
    row using row-2 machine fields. Example: {sheet:'(20) Ethernet Ports',
    match:{Port:'Ethernet1'}, values:{IpAddress:'192.168.1.10'}}. The template
    is never modified. additions can create an object safely by cloning exactly
    one complete existing row and overriding fields: {sheet, clone:{...},
    values:{Name:'NewObject', SequenceId:123}}. Hidden/required fields are
    retained from the source row. For a table with no prototype rows, pass
    {sheet, blank:true, values:{...}} to write a fully specified row. The
    project_settings mapping updates stable keys from the Project Settings
    sheet, for example {RtuProjectSettingsDataLoggingSelected:'Enabled <1>'}.
    The direct DTM import remains the semantic gate.
    """
    try:
        return create_workbook_copy(
            template_path,
            output_path,
            patches,
            additions=additions,
            project_settings=project_settings,
            overwrite=overwrite,
        )
    except (BulkEditError, OSError) as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool()
def create_remoteconnect_objects_copy(
    template_path: str,
    output_path: str,
    objects: list[dict],
    overwrite: bool = False,
) -> dict:
    """Create x70 objects and their dependent table relationships atomically.

    Each object requires name and an existing compatible prototype. Optional
    values override object fields. relationships may bind it to a Browser List,
    Modbus server status, Modbus scanner point, physical-I/O channel, DNP3 device
    status, DNP3 point, or existing data log. Sequence IDs are allocated
    automatically; scanner ranges expand when necessary. The produced workbook
    must pass cross-sheet validation and should then be applied with the strict
    native DTM import tool.
    """
    try:
        return create_objects_copy(
            template_path, output_path, objects, overwrite=overwrite
        )
    except (BulkEditError, OSError) as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool()
def create_remoteconnect_parent_entities_copy(
    template_path: str,
    output_path: str,
    browser_lists: list[dict] | None = None,
    data_logs: list[dict] | None = None,
    modbus_devices: list[dict] | None = None,
    modbus_scanners: list[dict] | None = None,
    dnp_devices: list[dict] | None = None,
    dnp_points: list[dict] | None = None,
    overwrite: bool = False,
) -> dict:
    """Create configuration parents before linking RemoteConnect objects.

    Supports Browser Lists, data logs, Modbus devices/scanners, and DNP3
    controlling-station devices/points. IDs and name references are allocated
    automatically. A compatible native row is cloned when available; an empty
    table receives a fully specified blank row. Use values for controller- and
    protocol-specific fields. Modbus devices use {name, prototype_name?, values};
    scanners use {device_name, register_start, register_quantity,
    prototype_scanner_sequence_id?, values?}. DNP3 devices use {name,
    prototype_name?, values:{DeviceAddress,...}}; points use {device_name,
    remote_point_number, point_type}, where point_type is digital_input,
    digital_output, analog_input, analog_output, or counter_input. Then apply
    the result through the strict native import/re-export gate. The template is
    never modified.
    """
    try:
        return create_parent_entities_copy(
            template_path,
            output_path,
            browser_lists=browser_lists,
            data_logs=data_logs,
            modbus_devices=modbus_devices,
            modbus_scanners=modbus_scanners,
            dnp_devices=dnp_devices,
            dnp_points=dnp_points,
            overwrite=overwrite,
        )
    except (BulkEditError, OSError) as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool()
def create_remoteconnect_configuration_project(
    output_path: str,
    controller: str = "SCADAPack x70",
    overwrite: bool = False,
) -> dict:
    """Create a new RemoteConnect PRJ from the installed DTM catalog.

    Creates and validates the CommDTM -> x70 controller topology without a
    template and without UI automation, then saves and reopens the new PRJ.
    """
    try:
        return create_configuration_project(
            output_path, controller=controller, overwrite=overwrite
        )
    except (NativeBridgeError, OSError, subprocess.SubprocessError) as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool()
def create_remoteconnect_project(
    project_path: str,
    platform: str,
    task: str = "MAST",
    section: str = "Main",
    st_source: str = "",
    variables: list[dict] | None = None,
    configuration_workbook: str = "",
    settings_xso: str = "",
    overwrite: bool = False,
) -> dict:
    """Create a complete paired RemoteConnect PRJ/STU project from scratch.

    project_path must end in .prj; the logic project is written beside it with
    the same stem and a .stu suffix. platform is the installed Logic Editor
    catalog family (SCADAPack47x, SCADAPack57x, or SCADAPack1070). The server
    must have CE_MCP_XPDF_CONTEXT set to the installed SCADAPack xpdf file.
    Optional variables require name/type and may include comment, address, and
    initial_value. When st_source is supplied, it is written to task/section.
    An optional native XLS can configure the new x70 DTM. Every enabled,
    logic-backed configuration object is automatically declared in the STU;
    explicit variables with the same name must use the exact configured DDT.
    Both outputs are staged, fully rebuilt, saved, and published only after all checks pass.
    No templates and no UI automation are used.
    """
    configuration_output = Path(project_path).expanduser().resolve()
    if configuration_output.suffix.lower() != ".prj":
        raise ValueError("project_path must end in .prj")
    logic_output = configuration_output.with_suffix(".stu")
    existing = [str(path) for path in (configuration_output, logic_output) if path.exists()]
    if existing and not overwrite:
        raise ValueError(
            "Output exists (set overwrite=true to replace both project files): "
            + ", ".join(existing)
        )
    allowed_platforms = {"SCADAPack47x", "SCADAPack57x", "SCADAPack1070"}
    if platform not in allowed_platforms:
        raise ValueError(f"platform must be one of {sorted(allowed_platforms)}")
    xpdf_context = os.environ.get("CE_MCP_XPDF_CONTEXT", "").strip()
    if not xpdf_context or not os.path.isfile(xpdf_context):
        raise ValueError(
            "CE_MCP_XPDF_CONTEXT must point to the installed SCADAPack .xpdf file "
            "before the MCP server starts"
        )

    requested_variables = list(variables or [])
    logic_sync = None
    if configuration_workbook:
        requested_variables, logic_sync = merge_configuration_logic_variables(
            configuration_logic_variable_specs(configuration_workbook),
            requested_variables,
        )

    created_variables: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="remoteconnect-full-project-") as temporary:
        stage = Path(temporary)
        staged_configuration = stage / configuration_output.name
        staged_logic = stage / logic_output.name
        configuration = create_configuration_project(str(staged_configuration))
        if configuration_workbook:
            imported_configuration = stage / (configuration_output.stem + "-configured.prj")
            imported = import_configuration_copy(
                str(staged_configuration),
                configuration_workbook,
                str(imported_configuration),
                require_clean=True,
            )
            if not imported["ok"] or not imported["published"]:
                return {
                    "ok": False,
                    "published": False,
                    "configuration": configuration,
                    "configuration_import": imported,
                    "reason": "The x70 DTM reported configuration errors; no project files were published.",
                }
            staged_configuration = imported_configuration
        else:
            imported = None

        try:
            logic = ce.new_logic_editor_project(
                platform, xpdf_context, settings_xso or None
            )
            for index, spec in enumerate(requested_variables):
                if not spec.get("name") or not spec.get("type"):
                    raise ValueError(f"variables[{index}] requires non-empty name and type")
                created_variables.append(ce.create_variable(
                    str(spec["name"]),
                    str(spec["type"]),
                    spec.get("comment"),
                    spec.get("address"),
                    spec.get("initial_value"),
                ))
            written = None
            if st_source.strip():
                written = ce.write_st_logic(task, section, st_source, None)
            build = ce.build_project(True)
            if build.get("build_state") != "built_ok":
                raise RuntimeError(f"Logic build did not succeed: {build}")
            saved = ce.save_project(str(staged_logic))
        finally:
            ce.close_project(False)

        if not staged_configuration.is_file() or not staged_logic.is_file():
            raise RuntimeError("Staged RemoteConnect project pair is incomplete")
        configuration_output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(staged_logic, logic_output)
        shutil.copy2(staged_configuration, configuration_output)

    return {
        "ok": True,
        "published": True,
        "configuration_path": str(configuration_output),
        "logic_path": str(logic_output),
        "platform": platform,
        "configuration": configuration,
        "configuration_import": imported,
        "logic": logic,
        "variables": created_variables,
        "configuration_logic_synchronization": logic_sync,
        "section": written,
        "build": build,
        "saved": saved,
        "headless": True,
        "ui_automation": False,
        "template_used": False,
    }


@mcp.tool()
def export_remoteconnect_configuration(
    source_path: str, output_path: str, overwrite: bool = False
) -> dict:
    """Export the x70 controller configuration to a native RemoteConnect XLS.

    Uses the installed FDT/DTM APIs headlessly; it does not launch, click, or
    automate RemoteConnect. The source PRJ is copied to temporary storage before
    the DTM is opened, so this read operation cannot rewrite the source project.
    """
    try:
        return export_configuration(source_path, output_path, overwrite=overwrite)
    except (NativeBridgeError, BulkEditError, OSError, subprocess.SubprocessError) as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool()
def apply_remoteconnect_configuration_copy(
    source_path: str,
    workbook_path: str,
    output_path: str,
    overwrite: bool = False,
    require_clean: bool = True,
) -> dict:
    """Import a native x70 configuration XLS and save a new PRJ copy.

    This is a fully programmatic FDT/DTM operation with no UI automation. The
    source is immutable. Import feedback is parsed into structured per-object
    errors; with require_clean=true (the default), the output is not published
    unless the DTM accepts the workbook, reports no errors, saves, and reopens.
    """
    try:
        return import_configuration_copy(
            source_path,
            workbook_path,
            output_path,
            overwrite=overwrite,
            require_clean=require_clean,
        )
    except (NativeBridgeError, BulkEditError, OSError, subprocess.SubprocessError) as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool()
def read_remoteconnect_import_report(log_path: str) -> dict:
    """Parse the UTF-16 diagnostic report from a headless x70 XLS import."""
    try:
        return read_import_report(log_path)
    except (FileNotFoundError, OSError) as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool()
def open_remoteconnect_logic(path: str) -> dict:
    """Open the paired UnitySoControl logic project for a RemoteConnect bundle.

    Pass any matching .PRJ/.RCZ/.STU/.STA path. Configure the SCADAPack OEM
    context with CE_MCP_XPDF_CONTEXT (and optionally CE_MCP_LIBSET_VERSION)
    before starting the server. The RemoteConnect configuration file is not
    modified by this operation.
    """
    bundle = discover_bundle(path)
    logic = bundle.get("logic_file")
    if not logic:
        raise ValueError(f"No paired .STU or .STA logic file found for: {path}")
    opened = ce.open_project(logic)
    return {"bundle": bundle, "logic": opened}


@mcp.tool()
def build_remoteconnect_logic_copy(
    source_path: str,
    output_path: str,
    task: str,
    section: str,
    st_source: str,
    variables: list[dict] | None = None,
    declare: dict[str, str] | None = None,
    overwrite: bool = False,
) -> dict:
    """Safely edit ST logic and save only after a clean build, always to a copy.

    source_path may be any member of a RemoteConnect bundle; its paired logic
    file is opened without saving in place. output_path must be a new .STU
    path unless overwrite=true. variables is an optional list of dictionaries
    with name/type and optional comment/address/initial_value. declare contains
    section-local or FB declarations. On edit/build failure the application is
    closed without saving and output_path is not written.
    """
    output = os.path.abspath(output_path)
    if os.path.splitext(output)[1].lower() != ".stu":
        raise ValueError("output_path must end in .stu")
    if os.path.exists(output) and not overwrite:
        raise ValueError(f"Output already exists (set overwrite=true to replace it): {output}")

    bundle = discover_bundle(source_path)
    logic = bundle.get("logic_file")
    if not logic:
        raise ValueError(f"No paired .STU or .STA logic file found for: {source_path}")

    created_variables: list[dict] = []
    try:
        opened = ce.open_project(logic)
        for spec in variables or []:
            if not spec.get("name") or not spec.get("type"):
                raise ValueError("Each variable requires non-empty name and type fields")
            created_variables.append(ce.create_variable(
                str(spec["name"]), str(spec["type"]),
                spec.get("comment"), spec.get("address"), spec.get("initial_value"),
            ))
        written = ce.write_st_logic(task, section, st_source, declare or None)
        build = ce.build_project(True)
        if build.get("build_state") != "built_ok":
            raise RuntimeError(f"Build did not succeed: {build}")
        os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
        saved = ce.save_project(output)
        return {
            "source_bundle": bundle,
            "opened": opened,
            "variables": created_variables,
            "section": written,
            "build": build,
            "saved": saved,
        }
    finally:
        ce.close_project(False)


@mcp.tool()
def new_project(cpu_part_number: str, cpu_version: str) -> dict:
    """Create a new project from scratch for a given PLC CPU.

    cpu_part_number must exactly match a CPU reference in the installed Control
    Expert hardware catalog, e.g. 'BMX P34 2020' (M340), 'BME P58 2040' (M580),
    'TSX P57 4634M' (Premium), '140 CPU 651 60' (Quantum). cpu_version is the
    firmware version offered by the catalog and is required, e.g. '02.70'.
    The project exists only in memory until save_project is called with a path;
    the project name is taken from that .stu file name (Control Expert has no
    separate, settable project-name field).
    """
    return ce.new_project(cpu_part_number, cpu_version)


@mcp.tool()
def save_project(path: str = "") -> dict:
    """Save the open project. Pass path (ending in .stu) on first save or to
    save a copy under a new name; leave empty to save in place."""
    return ce.save_project(path or None)


@mcp.tool()
def close_project(save: bool = False) -> dict:
    """Close the open project, optionally saving first. Unsaved changes are
    discarded when save=False."""
    return ce.close_project(save)


# ----------------------------------------------------------------- build


@mcp.tool()
def build_project(rebuild_all: bool = False) -> dict:
    """Build the project (incremental by default, full rebuild with
    rebuild_all=True). Returns the resulting build state and, when available,
    the Control Expert output window text with errors/warnings. A successful
    build is required before transferring to a PLC or simulator.

    On a fresh or just-imported project an incremental build has nothing to
    build against and Control Expert raises 'Build changes function not
    available'; this tool detects that and automatically performs a full
    rebuild instead (the result carries a 'note' when it does), so the default
    call works on the first build — you do not need to pass rebuild_all=True."""
    return ce.build_project(rebuild_all)


@mcp.tool()
def get_project_setting(ident: str) -> dict:
    """Read a project setting value by its identifier (e.g.
    'unity.multiAssign', 'unity.nestedComment', 'unity.RemanentLink'). Idents
    appear as <entryvalue ident="..."> in an exported .xef."""
    return ce.get_project_setting(ident)


@mcp.tool()
def set_project_settings(settings: dict) -> dict:
    """Set one or more project settings (build/language options). Common idents:
    'unity.multiAssign'='1' allows chained assignments a:=b:=c (error E1203 if
    disabled), 'unity.nestedComment', 'unity.paramNotAssign',
    'unity.emptyParaAllowed'. Values are strings ('0'/'1' for booleans). The
    project is reloaded from a patched archive and UNSAVED — build_project then
    save_project afterwards."""
    return ce.set_project_settings({str(k): str(v) for k, v in settings.items()})


@mcp.tool()
def analyze_project() -> dict:
    """Run a syntax/semantic analysis of the project without generating code.
    Faster than a build; use it to validate edits."""
    return ce.analyze_project()


# ------------------------------------------------------------- structure


@mcp.tool()
def get_project_structure() -> dict:
    """List the program structure: tasks (MAST, FAST, ...) with their scan
    settings and the sections in each task (name + IEC language), plus counts
    of events and functional modules."""
    return ce.get_project_structure()


@mcp.tool()
def list_data_types() -> dict:
    """List user data types in the project: DFB types (function blocks) and
    DDTs (structured/array types) with their versions."""
    return ce.list_data_types()


# -------------------------------------------------------------- variables


@mcp.tool()
def list_variables(name_filter: str = "", max_results: int = 200, offset: int = 0,
                   type_filter: str = "") -> dict:
    """List global variables with type, comment, topological address (%MW...,
    %I..., etc.), initial value and hardware_owner (I/O-managed variables).
    name_filter: case-insensitive substring of the name. type_filter: exact
    type name (e.g. 'BOOL', 'FC_AI', '_AI'). Results are paged: 'matched' is
    the number of hits, continue with offset=next_offset. Fast on large
    projects (one export instead of a per-variable walk)."""
    from .var_tools import list_variables as _list
    return ce._run(_list, ce, name_filter, max_results, offset, type_filter)


@mcp.tool()
def create_variable(
    name: str,
    type_name: str,
    comment: str = "",
    address: str = "",
    initial_value: str = "",
) -> dict:
    """Create a global variable.

    type_name is an IEC type ('BOOL', 'EBOOL', 'INT', 'DINT', 'REAL', 'TIME',
    'STRING', ...), an array ('ARRAY[0..9] OF INT'), a DDT or a DFB type name.
    address is an optional topological address like '%MW100' or '%Q0.2.0'.
    """
    return ce.create_variable(
        name, type_name, comment or None, address or None, initial_value or None
    )


@mcp.tool()
def update_variable(
    name: str,
    new_name: str = "",
    comment: str = "",
    address: str = "",
    initial_value: str = "",
) -> dict:
    """Update attributes of an existing variable. Only the non-empty arguments
    are applied. To clear an attribute, pass a single space."""
    return ce.update_variable(
        name,
        new_name or None,
        comment if comment else None,
        address if address else None,
        initial_value if initial_value else None,
    )


@mcp.tool()
def delete_variable(name: str) -> dict:
    """Delete a global variable by name."""
    return ce.delete_variable(name)


# --------------------------------------------------------------- sections


@mcp.tool()
def get_language_reference(language: str) -> dict:
    """REQUIRED READING before writing any program logic: the authoring guide
    for a Control Expert language ('ST', 'LD', 'FBD', 'SFC', 'IL').

    Returns the exchange-XML structure rules (validated against a live
    Control Expert build) and a complete example section that imports and
    builds with 0 errors — mirror its shapes exactly. Workflow: read this
    guide -> write the section XML -> import_xml(kind='section') ->
    build_project -> fix any errors listed in the build output -> repeat.
    For ST, prefer the write_st_logic tool (no XML needed).
    """
    from .lang_reference import REFERENCES

    ref = REFERENCES.get(language.upper())
    if ref is None:
        raise ValueError(f"Unknown language '{language}'. Use one of {sorted(REFERENCES)}.")
    return {"language": language.upper(), **ref}


# The same language references, also exposed as MCP resources so clients that
# browse resources (rather than call tools) discover the authoring guides. The
# get_language_reference tool remains the primary, tool-calling path.
@mcp.resource(
    "reference://languages",
    name="Control Expert language references",
    mime_type="text/plain",
    description="Index of the per-language authoring guides for writing program "
    "logic (also available via the get_language_reference tool).",
)
def _languages_index() -> str:
    from .lang_reference import REFERENCES

    lines = [
        "Control Expert program-language authoring references. Read one with the "
        "resource reference://language/<LANG> or the get_language_reference tool:",
    ]
    lines += [f"- reference://language/{lang}" for lang in sorted(REFERENCES)]
    return "\n".join(lines)


@mcp.resource(
    "reference://language/{language}",
    name="Control Expert language reference",
    mime_type="text/plain",
    description="Authoring guide and a validated, buildable example for one "
    "Control Expert language (ST, LD, FBD, SFC, IL).",
)
def _language_reference_resource(language: str) -> str:
    from .lang_reference import REFERENCES

    ref = REFERENCES.get(language.upper())
    if ref is None:
        raise ValueError(f"Unknown language '{language}'. Use one of {sorted(REFERENCES)}.")
    parts = [ref["guide"]]
    if ref.get("example"):
        parts.append("\nVALIDATED EXAMPLE (imports and builds with 0 errors):\n")
        parts.append(ref["example"])
    return "".join(parts)


@mcp.tool()
def write_st_logic(task: str, section: str, st_source: str, declare: str = "") -> dict:
    """Write a program section in plain IEC 61131-3 Structured Text — no XML
    required. Creates the section or replaces its logic if it exists.

    task is the program task that owns the section — 'MAST' for the default
    master task (also 'FAST', 'AUX0'..'AUX3', 'SAFE'); call
    get_project_structure to see the project's tasks. section is the section
    name to create or overwrite.
    st_source is raw ST (IF/CASE/FOR, FB calls like 'T1(IN := x, PT := t#3s,
    Q => y);', set()/reset() on EBOOLs, re()/fe() edges). declare optionally
    declares variables as a comma-separated 'name:TYPE' list, e.g.
    'StartPB:BOOL, Delay1:TON, Level:REAL' — FB instances called in the code
    (TON/TOF/CTU/DFB types) must exist or be declared here. Run build_project
    afterwards and fix any errors from its output.
    """
    decl = None
    if declare.strip():
        decl = {}
        for pair in declare.split(","):
            name, _, typ = pair.partition(":")
            if name.strip() and typ.strip():
                decl[name.strip()] = typ.strip()
    return ce.write_st_logic(task, section, st_source, decl)


@mcp.tool()
def place_fb_in_ladder(
    section: str, fb_type: str, instance_name: str, bindings: dict,
    rung_input: str = "", rung_contact: str = "", task: str = "MAST", pos_x: int = 2,
) -> dict:
    """Place a function block in LADDER from scratch — NO template needed — for
    any project DFB. Then build_project to confirm.

    An FFB in Ladder can't normally be hand-authored (CE owns the pin layout), but
    this generates the correct geometry automatically: it reads the DFB's pin
    interface, places the block (height = max(in,out)+1), wires ONE boolean input
    on its pin-row to the rail (EN left enabled), and binds the rest.

    fb_type: a project DFB type (e.g. 'FC_Valve', 'FC_VSD', 'Motor'). instance_name:
    the FB instance variable. bindings: {pin: variable_or_literal} for the data /
    in-out / output pins (e.g. {"Valve":"AV2","ExtReset":"0"}); auto-declared with
    the right types. rung_input: which BOOL input the rung drives (default: first
    boolean input). rung_contact: the BOOL variable feeding that input (default
    '<instance>_EN'). For elementary EFBs (TON/CTU/…) author in FBD instead.
    Multiple blocks = call once per block (each makes its own section)."""
    return ce.place_fb_in_ladder(
        task, section, fb_type, instance_name,
        {str(k): str(v) for k, v in bindings.items()},
        rung_input or None, rung_contact or None, pos_x,
    )


@mcp.tool()
def use_fb_in_ladder(
    template_section: str, new_section: str, instance_name: str,
    bindings: dict, task: str = "MAST",
) -> dict:
    """Place a function block in LADDER by cloning a GUI-authored FFB-in-LD
    template and rebinding it — then build_project to confirm.

    WHY: an FFB (EF/EFB/DFB) inside Ladder canNOT be hand-authored as XML — its
    pin columns come from Control Expert's GUI auto-layout, which import can't
    reproduce. So author the block in LD ONCE in the GUI (a 'template' section
    containing the FFBBlock), then this tool reuses that exact geometry and only
    swaps the instance name and pin variables. (For freely hand-authored block
    logic, use FBD instead.)

    template_section: name of the GUI-made LD section holding the block.
    new_section: name for the generated section (overwrites if it exists).
    instance_name: the new FB instance variable name.
    bindings: {pinName: variable_or_literal} to bind (e.g.
        {"Valve": "AV2", "ExtIntrlk": "Pump2_Intlk"}); pins you omit keep the
        template's bindings. Variable pins are auto-declared with the type the
        template used for that pin. task: the task of both sections (default MAST)."""
    return ce.clone_fb_in_ladder(
        task, template_section, new_section, instance_name,
        {str(k): str(v) for k, v in bindings.items()},
    )


@mcp.tool()
def read_section(task: str, section: str, max_chars: int = 60000) -> dict:
    """Read the logic of a program section as Control Expert XML. The XML
    contains the source code (ST text, ladder rungs, FBD networks...) and is
    the same format accepted by import_xml(kind='section').

    Prefer read_st_logic (ST, paged/searchable) or read_fbd_logic (FBD as
    blocks + wires): section XML gets very large (a 7,000-line ST section is
    ~1.3 MB). XML longer than max_chars is saved to a file instead: the reply
    then has 'saved_to' (full XML, usable with import_xml(file_path=...)),
    'xml_size' and the first 4,000 characters in 'xml_head'."""
    res = ce.read_section(task, section)
    xml = res.get("xml") or ""
    if max_chars and len(xml) > max_chars:
        path = ce._temp_path(f"_{section}.xml")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(xml)
        res = {k: v for k, v in res.items() if k != "xml"}
        res.update({"xml_size": len(xml), "saved_to": path, "xml_head": xml[:4000],
                    "hint": f"XML is {len(xml):,} chars (> max_chars={max_chars:,}); full text saved to "
                            "'saved_to'. Use read_st_logic / read_fbd_logic for a compact view."})
    return res


@mcp.tool()
def create_section(task: str, name: str, language: str = "ST") -> dict:
    """Create a new empty program section in a task.

    language: ST, LD, FBD, SFC, IL or LL984. To fill it with logic:
    - ST: call write_st_logic (plain IEC text, no XML) — easiest.
    - LD / FBD / SFC: FIRST call get_language_reference(language) to get the
      validated exchange-XML rules and a worked example, then author the
      section XML and import it with import_xml(kind='section'). SFC suits
      stepwise sequences.
    """
    return ce.create_section(task, name, language)


@mcp.tool()
def delete_section(task: str, section: str) -> dict:
    """Delete a program section from a task."""
    return ce.delete_section(task, section)


@mcp.tool()
def create_task(task_type: str, periodicity_ms: int = 0) -> dict:
    """Add a task to the project. task_type: MAST, FAST, AUX0..AUX3 or SAFE.
    periodicity_ms > 0 makes the task periodic with that period."""
    return ce.create_task(task_type, periodicity_ms or None)


# ---------------------------------------------------------- import/export


@mcp.tool()
def import_xml(
    kind: str,
    xml_content: str = "",
    file_path: str = "",
    task: str = "",
    import_mode: str = "overwrite",
) -> dict:
    """Import Control Expert XML into the open project — the main way to write
    LD/FBD/SFC program logic and bulk content.

    kind: 'section' (program logic, requires task), 'variables', 'dfb', 'ddt',
    'configuration', or 'project' (generic project-level import of an exchange
    file). Provide the XML either inline via xml_content or as file_path.
    import_mode: 'overwrite' (default), 'keep_existing' or 'rename'.

    Authoring graphical logic (kind='section'): for LD, FBD or SFC, FIRST call
    get_language_reference(language) and mirror its validated example, then
    validate_xml the candidate before importing. For Structured Text use
    write_st_logic instead (no XML needed). Always build_project afterwards and
    fix the per-section errors it lists.

    Tip: export an existing object first (read_section / export_xml) and use
    its XML as the structural template — Control Expert validates the schema
    strictly.
    """
    return ce.import_xml(xml_content or None, file_path or None, kind, task or None, import_mode)


_schema_dir: str | None = None


def _resolve_schema_dir() -> str | None:
    """Locate the SrcXmlSchema folder, caching the first success. The install
    path is fixed for the process, so this avoids a COM get_status() round-trip
    and a filesystem re-glob on every validate_xml call once resolved."""
    global _schema_dir
    if _schema_dir:
        return _schema_dir
    install = None
    try:
        install = ce.get_status().get("installation_path")
    except Exception:
        pass
    _schema_dir = ce_schema.find_schema_dir(install)
    return _schema_dir


@mcp.tool()
def validate_xml(xml_content: str = "", file_path: str = "") -> dict:
    """Validate candidate Control Expert exchange XML against the installed XSD
    grammar (the authoritative SrcXmlSchema) BEFORE import_xml / build_project.

    Catches structural errors (unknown elements, bad attribute enumerations,
    wrong ordering) instantly — far faster than the import+build round-trip — and
    steers authoring toward the full legal element set. Returns
    {valid, root, schema, errors}. For LD documents it additionally checks two
    semantic rules the grammar can't express: per-line cell-count arithmetic
    (each <typeLine>'s cell-spans must sum to nbColumns) and EBOOL-for-edge
    rules (P/N contacts and P/N coils need an EBOOL variable). Other semantics
    (FBD pin geometry, type compatibility) are still caught only by
    build_project, which remains the final oracle. Set CE_MCP_SCHEMA_DIR to
    override schema-folder auto-discovery."""
    if file_path and not xml_content:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            xml_content = f.read()
    if not xml_content.strip():
        raise ValueError("Provide xml_content or file_path.")
    sdir = _resolve_schema_dir()
    if not sdir:
        return {"valid": None, "error": "Could not locate SrcXmlSchema. "
                "Set CE_MCP_SCHEMA_DIR to the Control Expert SrcXmlSchema folder."}
    try:
        return {**ce_schema.validate(xml_content, sdir), "schema_dir": sdir}
    except ce_schema.SchemaError as exc:
        return {"valid": None, "error": str(exc)}


@mcp.tool()
def export_xml(kind: str, task: str = "", name: str = "") -> dict:
    """Export project content as Control Expert XML and return it inline.

    kind: 'variables' (all variables), 'program' (all program logic),
    'configuration' (hardware config), 'dfb' (one DFB type, requires name),
    'section' (one section, requires task and name). Large exports are written
    to a temp file and the path is returned instead.
    """
    return ce.export_xml(kind, task or None, name or None)


@mcp.tool()
def export_project(path: str) -> dict:
    """Export the full application to a .xef or .zef XML exchange file —
    useful for backup, diffing, or migrating between Control Expert versions."""
    return ce.export_project(path)


# --------------------------------------------------------- animation tables


@mcp.tool()
def list_animation_tables() -> dict:
    """List the project's animation tables (watch tables used to monitor and
    force variable values online)."""
    return ce.list_animation_tables()


@mcp.tool()
def create_animation_table(name: str, variables: str) -> dict:
    """Create an animation table (or add to an existing one) and fill it with
    variables to watch. variables is a comma-separated list of variable names,
    e.g. 'Motor, StartPB, UF1_Sequence.S_Service.x, Level'. With the project
    online (simulator or PLC), the table shows live values in the Control
    Expert UI."""
    var_list = [v.strip() for v in variables.split(",") if v.strip()]
    return ce.create_animation_table(name, var_list)


@mcp.tool()
def delete_animation_table(name: str) -> dict:
    """Delete an animation table by name."""
    return ce.delete_animation_table(name)


# ---------------------------------------------------------------- hardware


@mcp.tool()
def get_hardware() -> dict:
    """List the full hardware configuration: CPU plus the bus → drop → rack →
    module tree with part numbers, versions and topological addresses."""
    return ce.get_hardware()


@mcp.tool()
def add_io_module(
    part_number: str,
    slot: int,
    version: str = "02.00",
    rack: int = 0,
    drop: int = -1,
    bus: str = "",
) -> dict:
    """Add a hardware module (IO, communication, ...) to a rack slot.

    part_number and version must exactly match the hardware catalog for the
    PLC family, e.g. 'BMX DDI 1602' (16x DI), 'BMX DDO 1602' (16x DO),
    'BMX AMI 0410' (4x AI), 'BMX NOE 0100' (Ethernet) — version '02.00' fits
    most M340 IO modules ('01.00' for some; try both). slot is the rack
    position (the CPU usually occupies slot 0). rack/drop/bus default to the
    local rack on the local bus; use get_hardware to see the topology.
    """
    return ce.add_io_module(
        part_number, slot, version, rack, None if drop < 0 else drop, bus or None
    )


@mcp.tool()
def remove_io_module(slot: int, rack: int = 0, drop: int = -1, bus: str = "") -> dict:
    """Remove the module at a rack slot."""
    return ce.remove_io_module(slot, rack, None if drop < 0 else drop, bus or None)


@mcp.tool()
def add_drop(bus: str, drop: int, part_number: str, version: str = "01.00") -> dict:
    """Add a drop to a bus — e.g. a remote X80 EIO drop on the EIO/RIO bus of an
    M580 ('M580 Drop for Ethernet'). bus is a substring of the bus name from
    get_hardware (e.g. 'EIO'); drop is the drop topo number. Then use add_rack
    and add_io_module to populate it (remote racks need their own power
    supply)."""
    return ce.add_drop(bus, drop, part_number, version)


@mcp.tool()
def add_rack(bus: str, drop: int, rack: int, part_number: str, version: str = "01.00") -> dict:
    """Add a rack to a drop (e.g. 'BME XBP 1200' in a remote EIO drop). Then
    add a power supply and modules with add_io_module (set drop/bus to target
    the remote rack; remote drops start with a CRA adapter in slot 0)."""
    return ce.add_rack(bus, drop, rack, part_number, version)


@mcp.tool()
def replace_io_module(
    slot: int,
    old_part_number: str,
    old_version: str,
    new_part_number: str,
    new_version: str,
    rack: int = 0,
    drop: int = -1,
    bus: str = "",
) -> dict:
    """Replace a module in place (keeps the slot) — e.g. swap the default power
    supply new_project puts on the local rack. Old part number/version must
    match the current module exactly (see get_hardware)."""
    return ce.replace_io_module(
        slot, old_part_number, old_version, new_part_number, new_version,
        rack, None if drop < 0 else drop, bus or None,
    )


@mcp.tool()
def replace_rack(
    rack: int,
    old_part_number: str,
    old_version: str,
    new_part_number: str,
    new_version: str,
    drop: int = -1,
    bus: str = "",
) -> dict:
    """Replace a rack in place (e.g. the default BME XBP 0800 new_project
    creates -> BME XBP 0400). Modules on the rack are kept where the new rack
    has the same slots."""
    return ce.replace_rack(
        rack, old_part_number, old_version, new_part_number, new_version,
        None if drop < 0 else drop, bus or None,
    )


@mcp.tool()
def change_cpu(part_number: str, version: str) -> dict:
    """Replace the project's CPU with another from the hardware catalog
    (e.g. 'BMX P34 2020' + '02.70')."""
    return ce.change_cpu(part_number, version)


# -------------------------------------------------------------- DTM / Modbus


@mcp.tool()
def list_dtms() -> dict:
    """List the project's DTM topology (DTM Browser): communication/master
    DTMs and their slave devices, with names, DTM ids, types, and bus
    addresses (IP for Modbus TCP / EtherNet/IP devices)."""
    return ce.list_dtms()


@mcp.tool()
def add_dtm(
    device_type_name: str,
    dtm_name: str,
    parent_dtm: str = "",
    protocol_id: str = "",
    prog_id: str = "",
    version: str = "",
) -> dict:
    """Add a DTM to the project topology.

    Top-level communication DTM: leave parent_dtm empty (e.g. on M580 the CPU
    DTM 'BMEP58_ECPU_EXT' usually exists already). Slave device under a master
    DTM: set parent_dtm to the master's name and device_type_name to the
    catalog entry — e.g. add a generic Modbus TCP slave with
    device_type_name='Modbus Device' under the M580 CPU DTM. After adding,
    set its IP with set_dtm_address and configure scan requests with
    get/set_dtm_dataset or set_dtm_control_parameters.
    """
    return ce.add_dtm(
        device_type_name, dtm_name, parent_dtm or None, protocol_id or None,
        prog_id or None, version or None,
    )


@mcp.tool()
def delete_dtm(name: str) -> dict:
    """Delete a DTM (and its children) from the topology by name."""
    return ce.delete_dtm(name)


@mcp.tool()
def set_dtm_address(
    name: str, address: str = "", gateway: str = "", subnet: str = ""
) -> dict:
    """Set a slave DTM's bus address — for Modbus TCP / EtherNet/IP devices
    this is the IP address (e.g. '192.168.10.21'); for fieldbus slaves the
    node number. Optionally also set the device's gateway and subnet mask
    (the gateway must be in the same IP domain as the address or the build
    warns 'IP Address and Gateway address are not in the same domain').
    Setting gateway/subnet rewrites the project archive: the project is
    reloaded and UNSAVED — build_project then save_project afterwards."""
    return ce.set_dtm_address(name, address, gateway or None, subnet or None)


@mcp.tool()
def get_dtm_control_parameters(name: str) -> dict:
    """Read a master/communication DTM's control parameters as XML
    (CetControlParameterOutput schema): Modbus TCP I/O scanner lines with IP,
    unit id, timeouts, repetitive rate, and the ModbusTcpRequest entries
    (read/write start addresses and sizes)."""
    return ce.get_dtm_control_parameters(name)


@mcp.tool()
def set_dtm_control_parameters(name: str, xml: str, build: bool = True) -> dict:
    """Write a master DTM's control parameters (CetControlParameterInput XML)
    — scanner sizing and %MW mapping of the I/O scan. With build=True the
    DTM→PLC control information is rebuilt afterwards. Read the current
    values with get_dtm_control_parameters first and mirror the schema."""
    return ce.set_dtm_control_parameters(name, xml, build)


@mcp.tool()
def get_dtm_dataset(name: str) -> dict:
    """Export a slave DTM's configuration dataset for INSPECTION. The payload is
    a binary-framed FDT container (UTF-16 XML wrapped in length/GUID/type bytes),
    so the returned 'xml' has the framing stripped for reading and is NOT
    byte-faithful — do not edit it and write it back via set_dtm_dataset (the
    response carries a 'note' saying so). For M580 Modbus scan lines, use
    get_master_dtm_dataset / set_master_dtm_dataset instead."""
    return ce.get_dtm_dataset(name)


@mcp.tool()
def set_dtm_dataset(name: str, xml: str) -> dict:
    """Import a slave DTM configuration dataset (previously exported with
    get_dtm_dataset, possibly modified)."""
    return ce.set_dtm_dataset(name, xml)


@mcp.tool()
def get_master_dtm_dataset(dtm: str = "") -> dict:
    """Read the master/CPU DTM dataset XML — THE document holding the Modbus
    TCP scan lines (M580 DTMs XML Dataset spec).

    Each scanned device is a <SlaveDevice deviceTag="..."> under
    <SlaveDevices>; its <ModbusTCP><ManagedModbusRequestList> holds one
    <ManagedModbusRequest Index="N"> per scan line with Parameters attributes
    requestSettingBit (connection number), requestSettingRDAddress/RDLength,
    requestSettingWRAddress/WRLength, requestInputObjID/requestOutputObjID.
    dtm defaults to the first (CPU) DTM node.
    """
    return ce.get_master_dtm_dataset(dtm or None)


@mcp.tool()
def set_master_dtm_dataset(xml: str, dtm: str = "") -> dict:
    """Write back a modified master DTM dataset (adds/edits Modbus scan
    lines) and reload the project from the modified exchange archive.

    To add a Modbus scan request, insert into the target SlaveDevice's
    <ManagedModbusRequestList> a node shaped exactly like this (validated):

      <ManagedModbusRequest Index="0" State="0" FromExtTool="1">
        <Parameters requestSettingBit="1" requestSettingUnitID="255"
          requestSettingTimeout="1500" requestSettingRepetitiveRate="60"
          requestSettingRDAddress="0" requestSettingRDLength="10"
          requestSettingLastValue="0" requestSettingWRAddress="0"
          requestSettingWRLength="10" requestUniqueID="<NEW RANDOM GUID>"
          requestInputObjID="1024" requestOutputObjID="1025"
          requestReservedInputObjID="65535" requestReservedOutputObjID="1025"/>
      </ManagedModbusRequest>

    Rules (M580 CPU scanner): Index increments per request within the device
    starting at 0; requestSettingBit is the connection number (Modbus range
    1-128, unique across the scanner); requestUniqueID must be a fresh GUID;
    requestInput/OutputObjID use an even/odd pair (n, n+1) from 1024-1279 per
    connection, 65535 for an unused direction; RD/WR address+length are in
    words (slave-index format, 0-based). After this call the project is
    reloaded and UNSAVED — run build_project to validate (the scan line then
    appears in get_dtm_control_parameters), then save_project.
    """
    return ce.set_master_dtm_dataset(xml, dtm or None)


@mcp.tool()
def configure_cpu_ethernet(
    ip: str = "",
    subnet: str = "",
    gateway: str = "",
    ip_a: str = "",
    ip_b: str = "",
    ip_d: str = "",
    enable_tftp: int = -1,
    enable_eip: int = -1,
    enable_dhcp_bootp: int = -1,
    enable_ftp: int = -1,
    enable_web: int = -1,
    enable_snmp: int = -1,
) -> dict:
    """Configure the M580 CPU's embedded Ethernet: main IP/subnet/gateway
    (+ ip_a/ip_b for the HSBY A/B addresses and ip_d for address D) and the
    Security-screen service flags.

    Service args: -1 = leave unchanged, 0 = disable, 1 = enable. A remote EIO
    drop's CRA requires tftp, eip and dhcp_bootp ENABLED, otherwise the build
    fails with "CRA doesn't work when TFTP, EIP and DHCP_BOOTP settings are
    disabled". Updates every stored copy (application channel config words,
    DTM binary parameter container, DTM dataset XML) via a project-archive
    round-trip, so the project is reloaded and UNSAVED — build_project to
    validate, then save_project. For a slave device's gateway-domain warning
    use set_dtm_address(gateway=...) instead.
    """
    services: dict[str, bool] = {}
    for key, val in (
        ("tftp", enable_tftp), ("eipServer", enable_eip),
        ("dhcp_bootp", enable_dhcp_bootp), ("ftp", enable_ftp),
        ("webServer", enable_web), ("snmp", enable_snmp),
    ):
        if val in (0, 1):
            services[key] = bool(val)
    return ce.configure_cpu_ethernet(
        ip or None, subnet or None, gateway or None, ip_a or None, services,
        ip_b or None, ip_d or None,
    )


@mcp.tool()
def start_simulator(enforce_security: bool = False) -> dict:
    """Start the Control Expert PLC simulator (sim.exe) so plc_connect can
    reach it. With enforce_security=False (default) the simulator's 'use
    default application (enforce security)' option is disabled in the registry
    first — otherwise sim.exe blocks on a warning dialog unless a
    password-protected default application is configured. Only use on a local
    test machine."""
    return ce.start_simulator(enforce_security)


@mcp.tool()
def stop_simulator() -> dict:
    """Stop the PLC simulator process."""
    return ce.stop_simulator()


# ---------------------------------------------------------------- networks


@mcp.tool()
def list_networks() -> dict:
    """List logical networks (Premium/Quantum: Ethernet, Modbus Plus, Fipway)
    with their IP service configuration where available."""
    return ce.list_networks()


@mcp.tool()
def add_network(name: str, family: str) -> dict:
    """Create a logical network (Premium/Quantum platforms; family e.g.
    'Ethernet'). On M340/M580, configure communication on modules/DTMs
    instead."""
    return ce.add_network(name, family)


@mcp.tool()
def set_network_ip(name: str, ip_address: str, subnet_mask: str, gateway: str) -> dict:
    """Set the static IP configuration of a logical Ethernet network."""
    return ce.set_network_ip(name, ip_address, subnet_mask, gateway)


# --------------------------------------------------------------------- UI


@mcp.tool()
def show_ui(state: str = "show_normal", mode: str = "read_only", command_line: str = "") -> dict:
    """Make the Control Expert window of this automation session visible so a
    human can watch the AI work or take over. state: show_normal,
    show_maximized, minimize, restore.

    mode: 'read_only' (default — the GUI follows along while this client keeps
    the write token; required for opening editors) or 'read_write' (hand the
    GUI full control; only works if this client does not hold write access).
    command_line: optional Control Expert command line to open specific editors
    on startup."""
    return ce.show_ui(state, mode, command_line)


@mcp.tool()
def open_animation_table(name: str, state: str = "show_normal") -> dict:
    """Open an existing animation table's editor inside the live Control Expert
    window, so a human watches values update in real time while the AI drives a
    test. Typical live-test flow: create_animation_table -> start_simulator ->
    plc_connect -> plc_transfer -> plc_command('run') -> open_animation_table.
    The GUI opens read-only (this client keeps write); the table animates live
    once connected to the simulator/PLC."""
    return ce.open_animation_table(name, state)


# ----------------------------------------------------------------- online

if ONLINE_ENABLED:

    @mcp.tool()
    def plc_setup_connection(target: str = "simulator", address: str = "", driver: str = "") -> dict:
        """Configure the connection target. target: 'simulator' or 'plc'.
        address e.g. '127.0.0.1' or '192.168.10.1'; driver e.g. 'TCPIP'.
        EXPERIMENTAL — affects equipment when target='plc'."""
        return ce.plc_setup_connection(target, address or None, driver or None)

    @mcp.tool()
    def plc_connect(target: str = "simulator", mode: str = "primary") -> dict:
        """Connect to the PLC or simulator. The project must be built first."""
        return ce.plc_connect(target, mode)

    @mcp.tool()
    def plc_disconnect() -> dict:
        """Disconnect from the PLC/simulator."""
        return ce.plc_disconnect()

    @mcp.tool()
    def plc_state() -> dict:
        """Report connection state and PLC run/stop state."""
        return ce.plc_state()

    @mcp.tool()
    def plc_transfer(direction: str = "pc_to_plc") -> dict:
        """Transfer the application. direction: 'pc_to_plc' (download to
        controller — DANGEROUS on live equipment, PLC must be stopped) or
        'plc_to_pc' (upload from controller)."""
        return ce.plc_transfer(direction)

    @mcp.tool()
    def plc_command(command: str) -> dict:
        """Send a run/stop/init command to the connected PLC. DANGEROUS:
        starting or stopping a live controller affects the physical process."""
        return ce.plc_command(command)

    # ------------------------------------- live values over Modbus TCP

    @mcp.tool()
    def modbus_connect(host: str, port: int = 502, unit: int = 1, word_order: str = "low_first") -> dict:
        """Open a Modbus TCP link to a Modicon CPU's embedded server for live
        read/write of LOCATED tags (%M / %MW) while the PLC runs — the channel
        SCADA uses, and the only way this server reads/writes live values (the
        UDE/COM API cannot).

        Only located tags work: unlocated DFB internals (e.g. Pump1.Running)
        must be mirrored to a %MW/%M address in the program first. word_order is
        for 32-bit REAL/DINT: 'low_first' (Schneider default) or 'high_first'.
        Works against the SIMULATOR (host='127.0.0.1', the endpoint a Vijeo
        Designer I/O scanner reaches) as well as a real CPU IP; port 502."""
        return mb.connect(host, port, unit, word_order)

    @mcp.tool()
    def modbus_disconnect() -> dict:
        """Close the Modbus TCP connection."""
        return mb.disconnect()

    @mcp.tool()
    def modbus_status() -> dict:
        """Report the Modbus TCP connection state and decode settings."""
        return mb.status()

    @mcp.tool()
    def read_tags(tags: str) -> dict:
        """Read live values of located tags over Modbus TCP (modbus_connect
        first). tags is a comma-separated list; each item is a global variable
        name (its %address and IEC type are looked up from the project) or an
        explicit address '%MW70' / '%M3' / '%MW10.2', optionally typed
        '%MW2:REAL' or '%MW70:UDINT' (default INT for %MW, BOOL for %M).
        Returns {values:{tag:value}} and any per-tag errors."""
        plan = _build_tag_plan(tags.split(","))
        values, errors = {}, {}
        for label, addr, typ in plan:
            if not addr:
                errors[label] = "no located %M/%MW address (unknown name or unlocated tag)"
                continue
            try:
                values[label] = mb.read_one(addr, typ)
            except ModbusError as exc:
                errors[label] = str(exc)
        out: dict = {"values": values}
        if errors:
            out["errors"] = errors
        return out

    @mcp.tool()
    def write_tags(values: dict) -> dict:
        """Write live values to located tags over Modbus TCP (modbus_connect
        first). DANGEROUS — changes a running controller's process.

        values maps each tag (a global variable name, or an address like
        '%MW86' / '%M0' / '%MW2:REAL') to the value to write. Booleans go to %M
        coils or %MWi.j bits; numbers to %MW (INT, 1 word) or two %MW words
        (REAL/DINT/UDINT). Returns {written:{tag:value}} and any per-tag errors."""
        plan = _build_tag_plan(list(values.keys()))
        written, errors = {}, {}
        for label, addr, typ in plan:
            if not addr:
                errors[label] = "no located %M/%MW address (unknown name or unlocated tag)"
                continue
            try:
                mb.write_one(addr, typ, values[label])
                written[label] = values[label]
            except ModbusError as exc:
                errors[label] = str(exc)
        out: dict = {"written": written}
        if errors:
            out["errors"] = errors
        return out


def main() -> None:
    log.info(
        "Starting Control Expert MCP server (online tools %s)",
        "ENABLED" if ONLINE_ENABLED else "disabled — set CE_MCP_ENABLE_ONLINE=1 to enable",
    )
    mcp.run()


if __name__ == "__main__":
    main()
