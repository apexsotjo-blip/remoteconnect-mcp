"""Authoring reference for Control Expert program languages.

Served to AI clients via the get_language_reference tool. Every example here
was validated against Control Expert 14.0: imported via import_xml and built
with 0 errors. Geometry rules were derived from sections exported from the
official Schneider demo projects.
"""

ENVELOPE_GUIDE = """\
GENERAL RULES (all languages)
=============================
- A section file is: <XXExchangeFile> -> fileHeader, contentHeader,
  <program> (or <SFCProgram>) with <identProgram name="SectionName"
  type="section" task="MAST">, the language source, and an optional
  <dataBlock> declaring variables.
- fileHeader/contentHeader: copy the shapes from the examples verbatim
  (attribute values are not checked strictly, DTDVersion="41" matters).
- <dataBlock><variables name="X" typeName="BOOL"/></dataBlock> declares the
  variables the logic uses. Existing project variables do not need to be
  re-declared, but re-declaring identical ones is harmless. FB instances
  (TON, RS, CTU, DFB types...) must be declared as variables of the FB type.
- Import with import_xml(kind="section", xml_content=..., task="MAST").
  Importing a section that already exists deletes and replaces it.
- ALWAYS run build_project afterwards; the returned 'output' field contains
  per-section error lines like
    {FBD_Demo : [MAST]} : (l: 12, c: 7) E1189 converter error: ...
  Fix and re-import until 'Process succeeded'.
- Useful IEC types: BOOL, EBOOL (with edge memory; needed for P/N contacts
  and RE/FE), INT, DINT, UINT, REAL, TIME (literals like t#3s, t#500ms),
  STRING. Timers: TON/TOF/TP (IN, PT -> Q, ET). Counters: CTU/CTD.
  Bistables: RS (S, R1 -> Q1) / SR.
"""

ST_GUIDE = """\
ST (Structured Text) — easiest way to write logic
=================================================
Use the write_st_logic tool: pass plain IEC 61131-3 ST source, no XML needed.
Supported constructs: IF/ELSIF/ELSE, CASE, FOR, WHILE, REPEAT, function block
calls `MyTon(IN := x, PT := t#3s, Q => y);`, SET/RESET via `set(v);`/`reset(v);`
(EBOOL), edge functions re(v)/fe(v), step time access inside SFC actions
(`StepName.t`), arithmetic/comparison operators.
The XML envelope (used by import_xml/read_section) wraps the source in
<STSource> with XML-escaped text (&gt; for >, &amp; for &, &lt; for <).
"""

ST_EXAMPLE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<STExchangeFile>
\t<fileHeader company="Schneider Automation" product="Control Expert V14.0 - 190112" dateTime="date_and_time#2026-6-11-12:0:0" content="Structured source file" DTDVersion="41"></fileHeader>
\t<contentHeader name="Project" version="0.0.000"></contentHeader>
\t<program>
\t\t<identProgram name="Ctrl" type="section" task="MAST"></identProgram>
\t\t<STSource>(* motor seal-in with delay *)
StartDelay(IN := (StartPB OR Motor) AND NOT StopPB, PT := t#2s, Q =&gt; Motor);
RunLamp := Motor;
</STSource>
\t</program>
\t<dataBlock>
\t\t<variables name="StartPB" typeName="BOOL"></variables>
\t\t<variables name="StopPB" typeName="BOOL"></variables>
\t\t<variables name="Motor" typeName="BOOL"></variables>
\t\t<variables name="RunLamp" typeName="BOOL"></variables>
\t\t<variables name="StartDelay" typeName="TON"></variables>
\t</dataBlock>
</STExchangeFile>
"""

LD_GUIDE = """\
LD (Ladder) — line/cell model
=============================
<LDSource nbColumns="11"> (11..64) contains <networkLD> of <typeLine> rows.
CELL LAW: every <typeLine> must account for ALL nbColumns cells — the elements'
cell-spans sum to nbColumns (11 by default). The output (coil / control /
operateBlock) ends at the LAST column. Separate rungs with an empty line:
<typeLine><emptyLine nbRows="1"></emptyLine></typeLine>.

Contacts (1 cell each): <contact typeContact="X" contactVariableName="V"/>
  X = openContact (-| |-), closedContact (-|/|-), PContact (rising edge),
  NContact (falling edge). PContact/NContact REQUIRE V to be EBOOL (edge memory).
Links / spacing:
  <HLink nbCells="N"/> horizontal wire; <VLink/> vertical; <emptyCell nbCells="N"/>;
  <shortCircuit><VLink/><HLink nbCells="N"/></shortCircuit> = a ONE-row vertical
  link (fan one rung out to several coils, or merge a parallel branch); span more
  rows by placing consecutive <VLink/> cells below it.
Coils (LAST column): <coil typeCoil="X" coilVariableName="V"/>
  X = coil, notCoil, setCoil, resetCoil, PCoil, NCoil, callCoil, haltCoil.
  PCoil/NCoil REQUIRE V to be EBOOL.
Control (NOT coils — own element, LAST column):
  <control typeControl="jumpCoil" label="L"/> jumps to a label;
  <control typeControl="retCoil"/> returns from a subroutine (SR).
Jump target: <labelCell label="L"/> must sit ALONE on its own <typeLine>
  (pad it: <labelCell label="L"/><emptyCell nbCells="10"/>); the target rung is
  the NEXT line, connected to the left rail.
Inline blocks (span 'width' cells; hold ST text):
  <compareBlock width="W"><expression>Level &gt;= 50</expression></compareBlock>
    — a framed boolean test, used on the INPUT side like a contact.
  <operateBlock width="W"><statement>Output := Level * 2;</statement></operateBlock>
    — a framed assignment, used on the OUTPUT side like a coil.
  <FFBBlock .../> — an EF/EFB/DFB in a rung. Binding rules (if you must): leave
    boolean flow pins UNBOUND (binding EN errors E1228); bind data pins via
    effectiveParameter. BUT hand-authoring an FFB inside LD via XML is NOT
    reliable: the block's pin columns use a MIXED scale (width is in FBD units,
    position in LD cells), so the GUI auto-lays-out the pin coordinates and the
    connecting links — XML import needs pixel-accurate pin alignment and
    typically fails with E1217/E1218 (pins not connected to the rails).
    TO PLACE A DFB IN LADDER: use the place_fb_in_ladder tool — it generates the
    correct geometry automatically (NO template needed) by reading the DFB's pin
    interface, sizing the block (height = max(in,out)+1), wiring one boolean
    input on its pin-row to the rail and binding the rest. For elementary EFBs
    (TON/CTU/…) author them in FBD, or clone a GUI template with use_fb_in_ladder.
    Everything else in LD above (contacts, all coils, control/jump+label,
    compare/operate blocks, parallel branches) IS reliably hand-authored and
    build-validated.
"""

LD_EXAMPLE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<LDExchangeFile>
\t<fileHeader company="Schneider Automation" product="Control Expert V14.0 - 190112" dateTime="date_and_time#2026-6-11-12:0:0" content="Ladder source file" DTDVersion="41"></fileHeader>
\t<contentHeader name="Project" version="0.0.000"></contentHeader>
\t<program>
\t\t<identProgram name="LD_Demo" type="section" task="MAST"></identProgram>
\t\t<LDSource nbColumns="11">
\t\t\t<networkLD>
\t\t\t\t<typeLine>
\t\t\t\t\t<contact typeContact="openContact" contactVariableName="StartPB"></contact>
\t\t\t\t\t<contact typeContact="closedContact" contactVariableName="EStop"></contact>
\t\t\t\t\t<contact typeContact="PContact" contactVariableName="JogBtn"></contact>
\t\t\t\t\t<HLink nbCells="7"></HLink>
\t\t\t\t\t<coil typeCoil="setCoil" coilVariableName="Motor"></coil>
\t\t\t\t</typeLine>
\t\t\t\t<typeLine><emptyLine nbRows="1"></emptyLine></typeLine>
\t\t\t\t<typeLine>
\t\t\t\t\t<contact typeContact="NContact" contactVariableName="Trip"></contact>
\t\t\t\t\t<HLink nbCells="9"></HLink>
\t\t\t\t\t<coil typeCoil="notCoil" coilVariableName="Alarm"></coil>
\t\t\t\t</typeLine>
\t\t\t\t<typeLine><emptyLine nbRows="1"></emptyLine></typeLine>
\t\t\t\t<typeLine>
\t\t\t\t\t<compareBlock width="4"><expression>Level &gt;= 50</expression></compareBlock>
\t\t\t\t\t<HLink nbCells="6"></HLink>
\t\t\t\t\t<coil typeCoil="coil" coilVariableName="HighLevel"></coil>
\t\t\t\t</typeLine>
\t\t\t\t<typeLine><emptyLine nbRows="1"></emptyLine></typeLine>
\t\t\t\t<typeLine>
\t\t\t\t\t<contact typeContact="openContact" contactVariableName="Enable"></contact>
\t\t\t\t\t<HLink nbCells="6"></HLink>
\t\t\t\t\t<operateBlock width="4"><statement>Output := Level * 2;</statement></operateBlock>
\t\t\t\t</typeLine>
\t\t\t\t<typeLine><emptyLine nbRows="1"></emptyLine></typeLine>
\t\t\t\t<typeLine>
\t\t\t\t\t<contact typeContact="openContact" contactVariableName="SkipFlag"></contact>
\t\t\t\t\t<HLink nbCells="9"></HLink>
\t\t\t\t\t<control typeControl="jumpCoil" label="SKIP"></control>
\t\t\t\t</typeLine>
\t\t\t\t<typeLine><emptyLine nbRows="1"></emptyLine></typeLine>
\t\t\t\t<typeLine>
\t\t\t\t\t<labelCell label="SKIP"></labelCell>
\t\t\t\t\t<emptyCell nbCells="10"></emptyCell>
\t\t\t\t</typeLine>
\t\t\t\t<typeLine>
\t\t\t\t\t<contact typeContact="openContact" contactVariableName="RunReq"></contact>
\t\t\t\t\t<HLink nbCells="9"></HLink>
\t\t\t\t\t<coil typeCoil="coil" coilVariableName="RunOut"></coil>
\t\t\t\t</typeLine>
\t\t\t</networkLD>
\t\t</LDSource>
\t</program>
\t<dataBlock>
\t\t<variables name="StartPB" typeName="BOOL"></variables>
\t\t<variables name="EStop" typeName="BOOL"></variables>
\t\t<variables name="JogBtn" typeName="EBOOL"></variables>
\t\t<variables name="Motor" typeName="BOOL"></variables>
\t\t<variables name="Trip" typeName="EBOOL"></variables>
\t\t<variables name="Alarm" typeName="BOOL"></variables>
\t\t<variables name="Level" typeName="INT"></variables>
\t\t<variables name="HighLevel" typeName="BOOL"></variables>
\t\t<variables name="Enable" typeName="BOOL"></variables>
\t\t<variables name="Output" typeName="INT"></variables>
\t\t<variables name="SkipFlag" typeName="BOOL"></variables>
\t\t<variables name="RunReq" typeName="BOOL"></variables>
\t\t<variables name="RunOut" typeName="BOOL"></variables>
\t</dataBlock>
</LDExchangeFile>
"""

FBD_GUIDE = """\
FBD (Function Block Diagram) — blocks + links on a grid
=======================================================
PREFER write_fbd_logic: describe blocks and wires ({"name":"Delay","type":"TON",
"inputs":{"IN":"Ready.OUT","PT":"t#3s"},"outputs":{"Q":"PumpRun"}}) and the tool
computes every coordinate below, declares variables, imports and builds.
list_function_blocks / describe_function_block give exact pin names and types;
read_fbd_logic turns an existing FBD section into that description for editing.
Hand-written XML (rules below) is only needed for features the builder does not
emit (jumps/labels, named connectors, free comment placement).

<FBDSource nbRows="24" nbColumns="36"> contains <networkFBD> with:
  (ALWAYS nbRows="24" nbColumns="36", as Control Expert writes them — they are not the drawing
   size and blocks may sit far beyond row 24 / column 36. Other values make import fail with
   E1189 'Object creation error (invalid position)'. Keep the drawing compact instead: proven up to
   ~190 rows x ~120 columns; a single 250-row column failed — wrap into more columns.)
- <FFBBlock instanceName=".." typeName="AND_BOOL|OR_BOOL|TON|RS|CTU|<DFB>..."
  additionnalPinNumber="0" enEnO="false" width="8" height="H">
  * instanceName: ".1", ".2"... for stateless EF blocks (anonymous);
    for FB instances (TON, RS, DFBs) use a real name and DECLARE a variable
    of that type in dataBlock with the same name.
  * additionnalPinNumber: extra pins beyond the default 2 inputs
    (AND_BOOL with 3 inputs => additionnalPinNumber="1").
  * <descriptionFFB> lists EVERY pin in order: inputVariable EN, then inputs,
    then outputVariable ENO, then outputs. Bind a variable or literal with
    effectiveParameter="Var" (or t#3s etc.); invertedPin="true" puts a
    negation circle on the pin.
- GEOMETRY (matters!): height = 4 + number_of_visible_input_pins.
  Pin row i (0-based, inputs and outputs alike) = posY + 4 + i.
  Input pin column = posX. Output pin column = posX + width - 1.
- <linkFB><linkSource parentObjectName="block" pinName="OUT">
  <objPosition .../></linkSource><linkDestination parentObjectName="other"
  pinName="IN"><objPosition .../></linkDestination></linkFB>
  The objPosition of each end MUST equal the pin's grid position computed by
  the rule above, or import fails with E1189 'Link pin can not be located'.
  Optional <gridObjPosition> waypoints route bends.
- Leave 3+ columns between blocks; place blocks at increasing posX in
  execution order (left to right).
More network elements (all live directly inside <networkFBD>):
- <textBox width="W" height="H">Comment text<objPosition posX=".." posY=".."/>
  </textBox> — a free comment box. NOTE its coordinates use a FINER pixel-ish
  scale than the block grid (e.g. posX 20/150, not 3) — don't use the pin
  formula for it. A block may also carry its own <comment>..</comment> child.
- Control flow: <labelObject label="L"><objPosition .../></labelObject> as a jump
  target; <jumpObject label="L"><objPosition .../></jumpObject> to jump to it;
  <returnObject><objPosition .../></returnObject> to return from a subroutine.
- Named connectors instead of a drawn wire: on <linkFB> set
  connectorVisible="true" connectorText="Name" to label a long link.
"""

FBD_EXAMPLE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<FBDExchangeFile>
\t<fileHeader company="Schneider Automation" product="Control Expert V14.0 - 190112" dateTime="date_and_time#2026-6-11-12:0:0" content="Derived Function Block source file" DTDVersion="41"></fileHeader>
\t<contentHeader name="Project" version="0.0.000"></contentHeader>
\t<program>
\t\t<identProgram name="FBD_Demo" type="section" task="MAST"></identProgram>
\t\t<FBDSource nbRows="24" nbColumns="36">
\t\t\t<networkFBD>
\t\t\t\t<textBox width="220" height="15">Pump start interlock: ready AND NOT fault<objPosition posX="20" posY="2"></objPosition></textBox>
\t\t\t\t<textBox width="160" height="15">3 s on-delay before run<objPosition posX="150" posY="2"></objPosition></textBox>
\t\t\t\t<FFBBlock instanceName=".1" typeName="AND_BOOL" additionnalPinNumber="0" enEnO="false" width="8" height="6">
\t\t\t\t\t<objPosition posX="3" posY="3"></objPosition>
\t\t\t\t\t<descriptionFFB execAfter="">
\t\t\t\t\t\t<inputVariable invertedPin="false" formalParameter="EN"></inputVariable>
\t\t\t\t\t\t<inputVariable invertedPin="false" formalParameter="IN1" effectiveParameter="PumpReady"></inputVariable>
\t\t\t\t\t\t<inputVariable invertedPin="true" formalParameter="IN2" effectiveParameter="FaultActive"></inputVariable>
\t\t\t\t\t\t<outputVariable invertedPin="false" formalParameter="ENO"></outputVariable>
\t\t\t\t\t\t<outputVariable invertedPin="false" formalParameter="OUT"></outputVariable>
\t\t\t\t\t</descriptionFFB>
\t\t\t\t</FFBBlock>
\t\t\t\t<FFBBlock instanceName="StartDelay" typeName="TON" additionnalPinNumber="0" enEnO="false" width="8" height="6">
\t\t\t\t\t<objPosition posX="14" posY="3"></objPosition>
\t\t\t\t\t<descriptionFFB execAfter="">
\t\t\t\t\t\t<inputVariable invertedPin="false" formalParameter="EN"></inputVariable>
\t\t\t\t\t\t<inputVariable invertedPin="false" formalParameter="IN"></inputVariable>
\t\t\t\t\t\t<inputVariable invertedPin="false" formalParameter="PT" effectiveParameter="t#3s"></inputVariable>
\t\t\t\t\t\t<outputVariable invertedPin="false" formalParameter="ENO"></outputVariable>
\t\t\t\t\t\t<outputVariable invertedPin="false" formalParameter="Q" effectiveParameter="PumpRun"></outputVariable>
\t\t\t\t\t\t<outputVariable invertedPin="false" formalParameter="ET"></outputVariable>
\t\t\t\t\t</descriptionFFB>
\t\t\t\t</FFBBlock>
\t\t\t\t<linkFB>
\t\t\t\t\t<linkSource parentObjectName=".1" pinName="OUT">
\t\t\t\t\t\t<objPosition posX="10" posY="7"></objPosition>
\t\t\t\t\t</linkSource>
\t\t\t\t\t<linkDestination parentObjectName="StartDelay" pinName="IN">
\t\t\t\t\t\t<objPosition posX="14" posY="7"></objPosition>
\t\t\t\t\t</linkDestination>
\t\t\t\t</linkFB>
\t\t\t</networkFBD>
\t\t</FBDSource>
\t</program>
\t<dataBlock>
\t\t<variables name="PumpReady" typeName="BOOL"></variables>
\t\t<variables name="FaultActive" typeName="BOOL"></variables>
\t\t<variables name="PumpRun" typeName="BOOL"></variables>
\t\t<variables name="StartDelay" typeName="TON"></variables>
\t</dataBlock>
</FBDExchangeFile>
"""

SFC_GUIDE = """\
SFC (Sequential Function Chart) — steps, transitions, actions
=============================================================
Root is <SFCProgram areaNum="0" operatorCtrl="0"> (not <program>), containing
<chartSource name="Chart"><networkSFC> plus <actionSource> blocks.
Grid layout: steps sit on EVEN posY (0,2,4...), transitions on ODD posY
between them, same posX for a linear chain. Close the loop with
<jumpSFC stepName="FirstStep"/> after the last transition.
- <step stepType="initialStep|step" stepName="S_Name"> with <objPosition>.
  Exactly one initialStep per chart. Optional <action qualifier="NONE|N|S|R|
  P|P1|P0|L|D..."><actionName><sectionName>A_Name</sectionName></actionName>
  <tValue><tLiteral>t#5s</tLiteral></tValue></action> (tValue only for timed
  qualifiers L/D). Optional <literals max="t#..." min="t#..." delay=""/> for
  step supervision times (min = minimum step duration).
- <transition><objPosition .../><transitionCondition invertLogic="false">
  <variableName>BoolVar</variableName></transitionCondition></transition>
  The condition is a BOOL/EBOOL variable. For complex conditions compute a
  helper BOOL in an action or another section (e.g. 'FillDone := S_Fill.t
  >= FillTime AND LevelOK;'). 'StepName.t' gives the active step time and
  'StepName.x' the step active bit.
- Divergence/convergence (alternative branches): <altBranch width="W"
  relativePos="0"> at the branch row and <altJoint width="W"> at the join
  row; parallel: <parBranch>/<parJoint>. Branch columns: main at posX 0,
  alternatives at posX 3, 6, ...
- <actionSource name="A_Name"><STSource>...ST code...</STSource>
  </actionSource> — action sections run while their step is active
  (qualifier NONE/N). XML-escape the ST (&gt; etc.).
Note: SFC sections are allowed in the MAST task only.
"""

SFC_EXAMPLE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<SFCExchangeFile>
\t<fileHeader company="Schneider Automation" product="Control Expert V14.0 - 190112" dateTime="date_and_time#2026-6-11-12:0:0" content="SFC source file" DTDVersion="41"></fileHeader>
\t<contentHeader name="Project" version="0.0.000"></contentHeader>
\t<SFCProgram areaNum="0" operatorCtrl="0">
\t\t<identProgram name="SFC_Demo" type="section" task="MAST"></identProgram>
\t\t<chartSource name="Chart">
\t\t\t<networkSFC>
\t\t\t\t<step stepType="initialStep" stepName="S_Init">
\t\t\t\t\t<objPosition posX="0" posY="0"></objPosition>
\t\t\t\t\t<action qualifier="NONE">
\t\t\t\t\t\t<actionName>
\t\t\t\t\t\t\t<sectionName>A_Init</sectionName>
\t\t\t\t\t\t</actionName>
\t\t\t\t\t\t<tValue>
\t\t\t\t\t\t\t<tLiteral></tLiteral>
\t\t\t\t\t\t</tValue>
\t\t\t\t\t</action>
\t\t\t\t\t<literals max="" min="" delay=""></literals>
\t\t\t\t</step>
\t\t\t\t<step stepType="step" stepName="S_Fill">
\t\t\t\t\t<objPosition posX="0" posY="2"></objPosition>
\t\t\t\t\t<action qualifier="NONE">
\t\t\t\t\t\t<actionName>
\t\t\t\t\t\t\t<sectionName>A_Fill</sectionName>
\t\t\t\t\t\t</actionName>
\t\t\t\t\t\t<tValue>
\t\t\t\t\t\t\t<tLiteral></tLiteral>
\t\t\t\t\t\t</tValue>
\t\t\t\t\t</action>
\t\t\t\t\t<literals max="" min="t#1s" delay=""></literals>
\t\t\t\t</step>
\t\t\t\t<step stepType="step" stepName="S_Drain">
\t\t\t\t\t<objPosition posX="0" posY="4"></objPosition>
\t\t\t\t\t<literals max="" min="t#1s" delay=""></literals>
\t\t\t\t</step>
\t\t\t\t<transition>
\t\t\t\t\t<objPosition posX="0" posY="1"></objPosition>
\t\t\t\t\t<transitionCondition invertLogic="false">
\t\t\t\t\t\t<variableName>SeqStart</variableName>
\t\t\t\t\t</transitionCondition>
\t\t\t\t</transition>
\t\t\t\t<transition>
\t\t\t\t\t<objPosition posX="0" posY="3"></objPosition>
\t\t\t\t\t<transitionCondition invertLogic="false">
\t\t\t\t\t\t<variableName>FillDone</variableName>
\t\t\t\t\t</transitionCondition>
\t\t\t\t</transition>
\t\t\t\t<transition>
\t\t\t\t\t<objPosition posX="0" posY="5"></objPosition>
\t\t\t\t\t<transitionCondition invertLogic="false">
\t\t\t\t\t\t<variableName>DrainDone</variableName>
\t\t\t\t\t</transitionCondition>
\t\t\t\t</transition>
\t\t\t\t<jumpSFC stepName="S_Init">
\t\t\t\t\t<objPosition posX="0" posY="6"></objPosition>
\t\t\t\t</jumpSFC>
\t\t\t</networkSFC>
\t\t</chartSource>
\t\t<actionSource name="A_Init">
\t\t\t<STSource>FillDone := FALSE;
DrainDone := FALSE;
</STSource>
\t\t</actionSource>
\t\t<actionSource name="A_Fill">
\t\t\t<STSource>IF S_Fill.t &gt;= t#5s THEN
\tFillDone := TRUE;
END_IF;
IF S_Drain.t &gt;= t#3s THEN
\tDrainDone := TRUE;
END_IF;
</STSource>
\t\t</actionSource>
\t</SFCProgram>
\t<dataBlock>
\t\t<variables name="SeqStart" typeName="BOOL"></variables>
\t\t<variables name="FillDone" typeName="BOOL"></variables>
\t\t<variables name="DrainDone" typeName="BOOL"></variables>
\t</dataBlock>
</SFCExchangeFile>
"""

IL_GUIDE = """\
IL (Instruction List)
=====================
Same envelope as ST with root <ILExchangeFile> and source in <ILSource>.
Standard IEC IL: LD/LDN, AND/ANDN, OR/ORN, ST/STN, S, R, CAL, JMP...
Prefer ST for new logic; IL is provided for legacy compatibility.
"""

REFERENCES = {
    "ST": {"guide": ENVELOPE_GUIDE + "\n" + ST_GUIDE, "example": ST_EXAMPLE},
    "LD": {"guide": ENVELOPE_GUIDE + "\n" + LD_GUIDE, "example": LD_EXAMPLE},
    "FBD": {"guide": ENVELOPE_GUIDE + "\n" + FBD_GUIDE, "example": FBD_EXAMPLE},
    "SFC": {"guide": ENVELOPE_GUIDE + "\n" + SFC_GUIDE, "example": SFC_EXAMPLE},
    "IL": {"guide": ENVELOPE_GUIDE + "\n" + IL_GUIDE, "example": ""},
}

ST_ENVELOPE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<STExchangeFile>
\t<fileHeader company="Schneider Automation" product="Control Expert V14.0 - 190112" dateTime="date_and_time#2026-6-11-12:0:0" content="Structured source file" DTDVersion="41"></fileHeader>
\t<contentHeader name="Project" version="0.0.000"></contentHeader>
\t<program>
\t\t<identProgram name="{name}" type="section" task="{task}"></identProgram>
\t\t<STSource>{source}</STSource>
\t</program>
{datablock}</STExchangeFile>
"""
