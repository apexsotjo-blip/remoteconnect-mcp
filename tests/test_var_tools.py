"""Offline tests for the export-based variable listing."""
import unittest

from remoteconnect_mcp.var_tools import filter_page, parse_variables_export

XSY = """<VariablesExchangeFile><dataBlock>
<variables name="FCV_1_AutoPosSP" typeName="REAL">
  <comment>MV-1007 auto open position setpoint (%) &amp; more</comment>
  <variableInit value="50.0"></variableInit>
</variables>
<variables name="HL1000" typeName="_DI" topologicalAddress="%MW100">
  <comment>Float Switch High</comment>
  <instanceElementDesc name="Alarm"><comment>member comment</comment><value>TRUE</value></instanceElementDesc>
</variables>
<variables name="ECPU_IO" typeName="T_M_ECPU"><attribute name="Owner" value="0.0"></attribute></variables>
<variables name="FC_PT1004_0" typeName="FC_AI"></variables>
<variables name="Bare" typeName="BOOL"/>
</dataBlock></VariablesExchangeFile>"""


class VarToolsTests(unittest.TestCase):
    def test_parse(self):
        rows = {r["name"]: r for r in parse_variables_export(XSY)}
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows["FCV_1_AutoPosSP"]["initial_value"], "50.0")
        self.assertEqual(rows["FCV_1_AutoPosSP"]["comment"], "MV-1007 auto open position setpoint (%) & more")
        self.assertEqual(rows["HL1000"]["address"], "%MW100")
        self.assertEqual(rows["HL1000"]["comment"], "Float Switch High")   # not the member's comment
        self.assertEqual(rows["ECPU_IO"]["hardware_owner"], "0.0")
        self.assertEqual(rows["Bare"], {"name": "Bare", "type": "BOOL"})

    def test_filter_and_page(self):
        rows = parse_variables_export(XSY)
        r = filter_page(rows, max_results=2)
        self.assertEqual((r["total_variables"], r["matched"], r["returned"], r["next_offset"]), (5, 5, 2, 2))
        r2 = filter_page(rows, offset=r["next_offset"], max_results=10)
        self.assertNotIn("next_offset", r2)
        self.assertEqual(filter_page(rows, name_filter="fc")["matched"], 2)
        self.assertEqual([x["name"] for x in filter_page(rows, type_filter="fc_ai")["variables"]], ["FC_PT1004_0"])


if __name__ == "__main__":
    unittest.main()
