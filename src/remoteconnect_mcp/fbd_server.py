"""MCP tools for function-block knowledge and FBD authoring. Shared by control-expert-mcp and
remoteconnect-mcp: call register_fbd_tools(mcp, ce) from server.py."""
from __future__ import annotations

import json

from . import fbd_tools


def author_fbd_text(goal: str = "") -> str:
    """Body of the author_fbd MCP prompt (registered by each server's prompts.py)."""
    return (
        f"Goal: build an FBD section{(': ' + goal) if goal else ''}.\n\n"
        "1. Pick blocks: list_function_blocks(query='timer' / 'pid' / 'TO_REAL' / ...) — it covers "
        "library EFBs, elementary functions and the project's DFBs. describe_function_block(type) "
        "gives exact pin names + types. Never guess pin names.\n"
        "2. Describe the diagram for write_fbd_logic(section, blocks, task='MAST'):\n"
        "   - one entry per block: {name, type, inputs:{PIN: source}, outputs:{PIN: var}}\n"
        "   - source = variable | literal (t#3s, 250.0, TRUE) | wire 'Block.PIN'; prefix 'NOT ' "
        "to invert\n"
        "   - functions (AND_BOOL, ADD, GT ...) are anonymous: their name is only a wire label; "
        "EFB/DFB names become instance variables (declared automatically)\n"
        "   - extensible functions: just use IN3, IN4 ...\n"
        "   - no wire loops: feed back through a variable\n"
        "   - generic functions (ADD, MAX, LIMIT, SEL, MOVE) infer their type from input "
        "variables; otherwise pass declare={'Var': 'REAL'}\n"
        "3. Optional: dry_run=True first to review the layout without importing.\n"
        "4. Read the result: build_state must be 'built_ok' and section_messages empty. Each "
        "message names the block it points at — fix that block and call write_fbd_logic again "
        "(it replaces the section).\n"
        "5. To change an existing FBD section: read_fbd_logic → edit the description → "
        "write_fbd_logic (layout is regenerated).\n"
        "6. save_project when done."
    )


def _blocks_arg(blocks) -> list[dict]:
    if isinstance(blocks, str):
        blocks = json.loads(blocks)
    if not isinstance(blocks, list):
        raise ValueError("blocks must be a list of block objects.")
    return blocks


def register_fbd_tools(mcp, ce) -> None:
    @mcp.tool()
    def list_function_blocks(query: str = "", family: str = "", kind: str = "", limit: int = 100) -> dict:
        """Search every function and function block available for FBD/ST/LD: the installed
        library EFBs (timers, counters, SR/RS, triggers, SCALING, RAMP, PIDFF, PI_B, AUTOTUNE,
        TOTALIZER, HYST_*, LAG_FILTER, PWM1 ...), elementary functions (AND_BOOL, OR_BOOL,
        NOT_BOOL, ADD/SUB/MUL/DIV and typed variants, GT/GE/EQ/NE/LE/LT, SEL, LIMIT, MAX, MIN,
        MOVE, *_TO_* conversions) and the open project's DFBs.

        query: words to match in name/family/description, e.g. 'timer', 'pid', 'TO_REAL'.
        family: filter by family text, e.g. 'TIMERSANDCOUNTERS', 'CONT_CTL', 'Comparison'.
        kind: 'EF' (function, anonymous), 'EFB' (library block, needs an instance),
        'DFB' (project block). Returns inputs/outputs per block; call
        describe_function_block for pin types. With no query, also lists all families."""
        return ce._run(fbd_tools.list_function_blocks, ce, query, family, kind, limit)

    @mcp.tool()
    def describe_function_block(type_name: str) -> dict:
        """Exact pin interface of one function / function block: every input and output pin
        with its IEC type, whether it needs a named instance, extensible inputs (AND_BOOL
        IN3, IN4 ...), and a ready-to-fill write_fbd_logic block skeleton. Library EFBs come
        from the installed libset; DFBs are read from the open project; blocks missing from
        the catalog are read live from the open project's libset."""
        return ce._run(fbd_tools.describe_function_block, ce, type_name)

    @mcp.tool()
    def write_fbd_logic(section: str, blocks: list, task: str = "MAST", declare: dict | None = None,
                        comment: str = "", build: bool = True, dry_run: bool = False) -> dict:
        """Create or replace an FBD section from a simple block + wire description. The tool
        computes ALL geometry (block sizes, pin positions, links, bends, page size), numbers
        anonymous functions, declares new variables with the pin's type, imports the section
        and builds. No XML needed.

        blocks: list of {"name", "type", "inputs"?, "outputs"?, "comment"?, "pos"?}
          name    instance name for EFB/DFB types (becomes a variable); for functions
                  (AND_BOOL, ADD, GT ...) just a label used by wires.
          type    exact type from list_function_blocks, e.g. 'TON', 'AND_BOOL', 'FC_Valve'.
          inputs  {pin: source}. source = a variable ('PumpAuto'), a literal ('t#3s', '250.0',
                  'TRUE'), or a WIRE from another block's output '<block>.<PIN>'
                  ('Ready.OUT'). Prefix 'NOT ' to invert the pin ('NOT PumpFault',
                  'NOT Ready.OUT'). Extensible functions take IN3, IN4 ... automatically.
          outputs {pin: variable} to write an output to a variable. An output can feed
                  wires and be written to a variable at the same time.
          pos     optional [x, y] grid position. Blocks with pos keep it (read_fbd_logic returns
                  them, so editing a section keeps the engineer's drawing); blocks without pos are
                  placed automatically to the right of the kept ones.
          Pin values may also be ST expressions ('MBTV6.Closed AND NOT WP2.Auto'), bound as-is.
        Example:
          [{"name":"Ready","type":"AND_BOOL","inputs":{"IN1":"PumpAuto","IN2":"NOT PumpFault"}},
           {"name":"Delay","type":"TON","inputs":{"IN":"Ready.OUT","PT":"t#3s"},
            "outputs":{"Q":"PumpRun"}}]
        Blocks are laid out left to right in signal-flow order (wire loops are rejected —
        feed back through a variable). declare: {var: TYPE} for new variables whose type
        cannot be inferred (e.g. inputs of generic ADD/MAX). Existing project variables are
        never re-declared. build=True runs an incremental build and returns the messages of
        THIS section mapped to block names. dry_run=True returns the generated XML only."""
        return ce._run(fbd_tools.write_fbd_logic, ce, task, section, _blocks_arg(blocks),
                       declare or None, comment, build, dry_run)

    @mcp.tool()
    def read_st_logic(section: str, task: str = "MAST", start_line: int = 1, max_lines: int = 200,
                      find: str = "") -> dict:
        """Read an ST section as plain, line-numbered code — a page at a time — instead of the
        full exchange XML that read_section returns (a large section is 100+ KB of XML).
        start_line/max_lines page through it ('next_start_line' says where to continue). find
        returns only the lines containing that text (case-insensitive) with 2 lines of context,
        marked '>', e.g. find='P_01.Auto' to locate where a signal is used. To change code:
        read it, edit, and write the whole section back with write_st_logic."""
        from . import st_tools
        return ce._run(st_tools.read_st_logic, ce, task, section, start_line, max_lines, find)

    @mcp.tool()
    def read_fbd_logic(section: str, task: str = "MAST") -> dict:
        """Read an existing FBD section back as the same block + wire description that
        write_fbd_logic accepts (variables, literals, wires 'Block.PIN', inverted pins).
        Edit it and write it back to change an FBD section without touching XML. Anonymous
        functions get readable names like AND_BOOL_1. Each block carries its drawing position
        ('pos'), so writing the edited description back keeps the existing layout; delete the
        'pos' entries to have the whole sheet redrawn."""
        return ce._run(fbd_tools.read_fbd_logic, ce, task, section)
