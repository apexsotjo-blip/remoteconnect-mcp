"""Guided-workflow prompts for the RemoteConnect MCP server.

MCP prompts are reusable recipes a client can invoke (in Claude Code they show
up as /mcp__control-expert__<name> slash commands). They encode the validated
flows — and the non-obvious gotchas — so a client doesn't rediscover them by
trial and error.

CONVENTION FOR CONTRIBUTORS: when you add a tool or capability, add (or extend)
a prompt here that walks a client through using it, especially any step that is
not guessable from the tool description alone (an env flag, a manual GUI step,
an ordering constraint, an addressing rule). A good prompt turns an hour of
trial-and-error into one slash command. See README "Extending the server".

All functions return a plain instruction string (FastMCP delivers it as the
prompt's user message). Register them with register_prompts(mcp).
"""

from __future__ import annotations

from .fbd_server import author_fbd_text


def register_prompts(mcp) -> None:
    @mcp.prompt()
    def inspect_remoteconnect_configuration() -> str:
        """Inventory RemoteConnect configuration and interpret edit feedback safely."""
        return (
            "Goal: understand a RemoteConnect project configuration and its edit feedback.\n\n"
            "1. Call inspect_remoteconnect_project(path) to locate the complete PRJ/STU bundle.\n"
            "2. Call audit_remoteconnect_project(path) to verify PRJ object names/DDTs against "
            "STU variables, section references, native relationships, live-editor rules, and "
            "an unsaved full rebuild. Resolve critical synchronization errors before editing.\n"
            "3. Call inventory_remoteconnect_configuration(path) to enumerate DTM topology IDs "
            "and every configuration record group without deserializing proprietary payloads.\n"
            "4. Call export_remoteconnect_configuration to create the DTM's native XLS from a "
            "temporary project copy, then inspect its stable row-2 fields.\n"
            "5. Call inspect_remoteconnect_configuration_relationships on the export. For plain "
            "field edits use create_remoteconnect_bulk_edit_copy. Create missing Browser Lists, "
            "data logs, Modbus devices/scanners, and DNP3 devices/points with "
            "create_remoteconnect_parent_entities_copy. Then pass that output to "
            "create_remoteconnect_objects_copy with compatible prototypes and explicit browser, "
            "physical-I/O, DNP3, Modbus, or data-log relationships. Data-log creation/linking "
            "automatically enables the project-level logging switch. Both tools allocate IDs "
            "and validate "
            "foreign keys; the composed workbook needs only one final native import. Always use "
            "an XLS exported from the exact PRJ that will be the import source; Replace-all "
            "workbooks are project-specific.\n"
            "6. Call apply_remoteconnect_configuration_copy with require_clean=true. It invokes "
            "the x70 DTM directly, parses its UTF-16 report, saves/reopens, and publishes a new "
            "PRJ only when ok=true. A post-import native re-export also verifies that requested "
            "additions really survived; a zero-error DTM log alone is not sufficient. Repair "
            "per-object errors and repeat. No UI automation is used."
        )

    @mcp.prompt()
    def create_complete_remoteconnect_project() -> str:
        """Create configuration and clean-built logic as a paired project."""
        return (
            "Goal: create a complete RemoteConnect application from scratch.\n\n"
            "1. Ensure CE_MCP_XPDF_CONTEXT points to SCBForSCADAPack.xpdf.\n"
            "2. Call create_remoteconnect_project with a new .prj path, the installed platform "
            "(SCADAPack47x/57x/1070), variables, and ST source. Optionally pass a native XLS.\n"
            "Configuration-backed object variables are generated in the STU automatically; "
            "an explicit declaration with the same name must use its exact configured DDT.\n"
            "3. Accept the result only when ok=true, published=true, the DTM topology reopened "
            "with two DTMs, and build.build_state is built_ok. The matching .stu is published "
            "beside the .prj.\n"
            "4. For LD/FBD/SFC/IL additions, open the generated .stu, call the appropriate "
            "language reference, import the section XML, rebuild, and save a reviewed copy.\n\n"
            "The operation is headless and template-free; staged files are discarded on failure."
        )

    @mcp.prompt()
    def edit_remoteconnect_logic_copy() -> str:
        """Safely edit and rebuild RemoteConnect logic without touching the source."""
        return (
            "Goal: change a RemoteConnect SCADAPack logic project safely.\n\n"
            "1. inspect_remoteconnect_project(path) to confirm the paired PRJ/STU files.\n"
            "2. Call build_remoteconnect_logic_copy with a NEW output_path, task, section, "
            "ST source, and optional variables/declarations.\n"
            "3. The tool opens the source without saving it, applies edits in memory, performs "
            "a full rebuild, and writes output_path only when build_state is 'built_ok'.\n"
            "4. Open the resulting copy with open_project and use read_section/list_variables "
            "for independent verification before replacing or importing anything manually.\n\n"
            "GOTCHA: the server must start with CE_MCP_XPDF_CONTEXT pointing to the installed "
            "SCBForSCADAPack.xpdf. The tool saves IEC logic only; it does not rewrite the "
            "paired RemoteConnect PRJ/RCZ configuration. Keep overwrite=false unless replacing "
            "a specifically reviewed generated copy."
        )

    @mcp.prompt()
    def commission_simulator() -> str:
        """Bring the open project up and running on the Control Expert PLC simulator."""
        return (
            "Goal: get the currently open project running on the Control Expert simulator.\n\n"
            "1. build_project — must reach build_state 'built_ok' before any transfer "
            "(fix errors from the 'output' field and rebuild).\n"
            "2. start_simulator — launches sim.exe (loopback).\n"
            "3. plc_setup_connection(target='simulator', address='127.0.0.1', driver='TCPIP').\n"
            "4. plc_connect(target='simulator').\n"
            "5. plc_command('stop')  — a transfer is rejected if the PLC is running.\n"
            "6. plc_transfer(direction='pc_to_plc').\n"
            "7. plc_command('run'); confirm with plc_state (expect plc_state='run', "
            "pc_equals_plc=true).\n\n"
            "GOTCHA — 'Family check failed' on plc_transfer: the API download requires the "
            "simulator to ALREADY hold a station of the SAME CPU family. A fresh sim "
            "(plc_state='no_conf') or one running a different-family station rejects it. "
            "Seed it ONCE manually in the Control Expert GUI (PLC -> Simulation Mode -> "
            "Connect -> Transfer); the station persists while sim.exe runs and API "
            "downloads work afterwards. This also covers M580 Hot-Standby CPUs.\n"
            "NOTE: requires the server started with CE_MCP_ENABLE_ONLINE=1 (the plc_* tools "
            "are hidden otherwise)."
        )

    @mcp.prompt()
    def test_logic_live(instance: str = "Pump1", task: str = "MAST") -> str:
        """Live-test a DFB instance by writing inputs and reading outputs over Modbus TCP."""
        return (
            f"Goal: drive and observe the '{instance}' logic live on the running PLC/simulator.\n\n"
            "Live values travel over Modbus TCP (the UDE/COM API has NO live tag access). "
            "First make sure the project is RUNNING on the target — see the "
            "commission_simulator prompt.\n\n"
            "KEY CONSTRAINT: Modbus only reaches LOCATED tags (%M / %MW). A DFB instance's "
            f"fields (e.g. {instance}.Running, {instance}.ManStartCmd, {instance}.SpdRefAO) are "
            "UNLOCATED internals and are invisible to Modbus until mirrored. So:\n\n"
            f"1. Add a small mirror section in task '{task}' (write_st_logic) that bridges the "
            "fields you want to drive/observe to located addresses, e.g.:\n"
            f"     {instance}.ManStartCmd := %MX100.0;   {instance}.Auto := %MX100.1;\n"
            f"     %MX110.0 := {instance}.Running;  %MX110.1 := {instance}.Starting;\n"
            f"     %MX110.2 := {instance}.FTS_Alarm;  %MW111 := REAL_TO_INT({instance}.SpdScaled);\n"
            "   Declare the %MX/%MW tags (create_variable) or use existing located vars.\n"
            "2. build_project, then transfer + run (commission_simulator).\n"
            "3. modbus_connect(host='127.0.0.1')  (or the real CPU IP; port 502).\n"
            "4. write_tags({'%MX100.1': false, '%MX100.0': true})  — set manual mode, then start.\n"
            "5. read_tags('%MX110.0, %MX110.1, %MX110.2, %MW111')  — watch the state machine "
            "react (Stopped -> Starting -> Running; SpdScaled tracking).\n\n"
            "GOTCHA — 32-bit REAL/DINT word order: default is Schneider 'low_first'. If a REAL "
            "reads garbage, reconnect with word_order='high_first'.\n"
            "Optional: create_animation_table + open_animation_table to watch the same tags "
            "live in the Control Expert window while you drive them."
        )

    @mcp.prompt()
    def author_logic(language: str = "ST") -> str:
        """Write a program section in a given IEC language and get it building clean."""
        lang = (language or "ST").upper()
        common = (
            "Loop until the build is clean:\n"
            "  a. (graphical/XML) call validate_xml FIRST — it checks the document against "
            "the installed CE schema and flags unknown elements / bad attribute values "
            "instantly, before the slower build.\n"
            "  b. import_xml the section (or write_st_logic for ST).\n"
            "  c. build_project; read the 'output' field — it lists per-section errors "
            "(pin geometry, type rules — things the schema can't catch); fix and repeat "
            "until 'Process succeeded'.\n"
            "Declare referenced variables first (create_variable) or include a <dataBlock>.\n"
        )
        if lang == "ST":
            return (
                "Goal: author a Structured Text section.\n\n"
                "Use write_st_logic(task, section, st_source, declare='name:TYPE, ...') — plain "
                "IEC text, no XML. FB instances (TON/TOF/CTU/DFB types) must be declared.\n\n" + common
            )
        if lang == "FBD":
            return author_fbd_text()
        return (
            f"Goal: author a {lang} section (graphical/textual).\n\n"
            f"1. get_language_reference('{lang}') FIRST — it returns the exchange-XML structure "
            "rules and a validated example that builds with 0 errors. Mirror its shapes exactly "
            "(FBD pin geometry, LD cell model, SFC chart layout).\n"
            "2. import_xml(kind='section', task=..., xml_content=...).\n" + common +
            "SFC is the right choice for stepwise sequences (steps + transitions + ST actions)."
        )

    @mcp.prompt()
    def author_fbd(goal: str = "") -> str:
        """Design and wire an FBD section from blocks and wires, then get it building clean."""
        return author_fbd_text(goal)

    @mcp.prompt()
    def scaffold_project(cpu_part_number: str = "BME P58 2040", cpu_version: str = "") -> str:
        """Create a new project for a CPU and build out a starter rack/IO configuration."""
        ver = cpu_version or "<exact catalog version, e.g. 02.70 / 03.20>"
        return (
            f"Goal: scaffold a new project on CPU '{cpu_part_number}'.\n\n"
            f"1. new_project(cpu_part_number='{cpu_part_number}', cpu_version='{ver}'). The part "
            "number AND version must EXACTLY match this Control Expert version's hardware catalog "
            "(spacing matters); 'Catalog object not found' means no exact match — verify in the GUI.\n"
            "2. get_hardware to see the auto-created local rack/PSU/CPU topology.\n"
            "3. add_io_module(part_number, slot, version) for each module (version is mandatory: "
            "'02.00' for most M340 IO, '01.00' for racks/PSUs; try both on failure). Use "
            "add_drop/add_rack for remote EIO drops.\n"
            "4. create_variable / write_st_logic to add data and logic (see author_logic).\n"
            "5. build_project until 'built_ok', then save_project('C:/path/MyApp.stu').\n\n"
            "Tip: prefer a standalone CPU for simulator work; HSBY needs a manual first transfer "
            "(see commission_simulator)."
        )

    @mcp.prompt()
    def fb_in_ladder(block_type: str = "TON") -> str:
        """Place a function block inside a Ladder section (which can't be hand-authored)."""
        return (
            f"Goal: use a function block ({block_type}) inside LADDER logic.\n\n"
            "An FFB in LD can't be hand-authored as XML (CE's GUI owns the pin layout), but "
            "the server generates it for you. Routes, best first:\n\n"
            f"ROUTE A — place_fb_in_ladder (project DFBs like {block_type}, NO template):\n"
            "  place_fb_in_ladder(section, fb_type, instance_name, bindings={'Pin':'Var', ...},\n"
            "    rung_input=<bool input the rung drives, optional>, rung_contact=<bool var>) —\n"
            "  reads the DFB interface and generates correct geometry automatically; then\n"
            "  build_project. Call once per block (each makes its own section). This is the\n"
            "  easy path for your DFBs (FC_Valve, FC_VSD, Motor, ...).\n"
            "ROUTE B — elementary EFBs (TON/CTU/…): author them in FBD (author_logic, "
            "language='FBD'), or make a GUI template once and clone with use_fb_in_ladder.\n"
            "ROUTE C — template clone: GUI-author a block-in-LD section once, then "
            "use_fb_in_ladder(template_section, new_section, instance_name, bindings).\n\n"
            "Everything else in Ladder (contacts incl. P/N edges, all coil types, jump/label, "
            "compareBlock/operateBlock, parallel branches) IS hand-authorable — see "
            "get_language_reference('LD')."
        )

    @mcp.prompt()
    def add_modbus_device(parent_dtm: str = "BMEP58_ECPU_EXT") -> str:
        """Add a Modbus-TCP slave device under an M580 CPU DTM and configure a scan line."""
        return (
            "Goal: add a Modbus TCP slave to the M580 scanner and give it a read/write scan line.\n\n"
            f"1. add_dtm(device_type_name='Modbus Device', dtm_name='MyDevice', "
            f"parent_dtm='{parent_dtm}')  — protocol_id 'Modbus' is auto-tried.\n"
            "2. set_dtm_address('MyDevice', address='192.168.10.21', gateway=..., subnet=...) "
            "(gateway must share the IP domain or the build warns).\n"
            "3. get_master_dtm_dataset to see the <SlaveDevices>/<ManagedModbusRequestList>; add a "
            "<ManagedModbusRequest> (mirror the template + numbering rules in the "
            "set_master_dtm_dataset tool description: Index from 0, unique requestSettingBit "
            "connection number, fresh GUID requestUniqueID, even/odd Input/OutputObjID pair).\n"
            "4. set_master_dtm_dataset(xml) — reloads the project UNSAVED.\n"
            "5. build_project (the line appears in get_dtm_control_parameters as a ModbusScanLine), "
            "then save_project."
        )
