"""Offline tests for the FBD builder and catalog (no Control Expert needed).
The geometry rules asserted here were validated by import + build on Control Expert 16.2."""
import re
import unittest

from remoteconnect_mcp import ffb_catalog
from remoteconnect_mcp.fbd_builder import FbdError, block_at, build_fbd_section

SPECS = {e["name"]: ffb_catalog.to_spec(e) for e in ffb_catalog.entries() if e["pins"]}


def pin_xy(xml, parent, pin, end):
    m = re.search(rf'<{end} parentObjectName="{re.escape(parent)}" pinName="{pin}">\s*'
                  r'<objPosition posX="(\d+)" posY="(\d+)"', xml)
    return int(m.group(1)), int(m.group(2))


class FbdBuilderTests(unittest.TestCase):
    def build(self, blocks, known=None, declare=None):
        return build_fbd_section("S", "MAST", blocks, SPECS, known or {}, declare)

    def test_pin_geometry_matches_control_expert_rule(self):
        r = self.build([{"name": "A", "type": "AND_BOOL", "inputs": {"IN1": "x", "IN2": "y", "IN3": "z"}},
                        {"name": "T", "type": "TON", "inputs": {"IN": "A.OUT", "PT": "t#1s"}}])
        a, t = r["layout"]
        self.assertEqual(a["height"], 4 + 3)                      # 4 + inputs
        self.assertEqual(pin_xy(r["xml"], ".1", "OUT", "linkSource"), (a["x"] + a["width"] - 1, a["y"] + 4))
        self.assertEqual(pin_xy(r["xml"], "T", "IN", "linkDestination"), (t["x"], t["y"] + 4))
        self.assertIn('additionnalPinNumber="1"', r["xml"])         # AND_BOOL with IN3

    def test_functions_are_anonymous_and_instances_declared(self):
        r = self.build([{"name": "A", "type": "AND_BOOL", "inputs": {"IN1": "x", "IN2": "y"}},
                        {"name": "Tmr", "type": "TON", "inputs": {"IN": "A.OUT"}, "outputs": {"Q": "q"}}])
        self.assertIn('instanceName=".1" typeName="AND_BOOL"', r["xml"])
        self.assertEqual(r["declared"], {"x": "BOOL", "y": "BOOL", "Tmr": "TON", "q": "BOOL"})

    def test_existing_variables_are_not_redeclared(self):
        r = self.build([{"name": "A", "type": "AND_BOOL", "inputs": {"IN1": "x", "IN2": "y"}}], known={"X": "BOOL"})
        self.assertNotIn("x", r["declared"])

    def test_inverted_wire_and_inverted_variable(self):
        r = self.build([{"name": "A", "type": "AND_BOOL", "inputs": {"IN1": "x", "IN2": "NOT y"}},
                        {"name": "S", "type": "SR", "inputs": {"S1": "NOT A.OUT", "R": "r"}}])
        self.assertIn('invertedPin="true" formalParameter="IN2" effectiveParameter="y"', r["xml"])
        self.assertIn('invertedPin="true" formalParameter="S1"></inputVariable>', r["xml"])
        self.assertEqual(r["wires"], ["A.OUT -> S.S1 (inverted)"])

    def test_generic_function_type_inferred_from_inputs(self):
        r = self.build([{"name": "Sum", "type": "ADD", "inputs": {"IN1": "f1", "IN2": "f2"}, "outputs": {"OUT": "tot"}}],
                       declare={"f1": "REAL", "f2": "REAL"})
        self.assertEqual(r["declared"]["tot"], "REAL")

    def test_generic_without_type_asks_for_declare(self):
        with self.assertRaisesRegex(FbdError, "declare"):
            self.build([{"name": "M", "type": "MAX", "inputs": {"IN1": "a", "IN2": "b"}}])

    def test_columns_follow_signal_flow(self):
        r = self.build([{"name": "Last", "type": "TON", "inputs": {"IN": "Mid.OUT"}},
                        {"name": "Mid", "type": "OR_BOOL", "inputs": {"IN1": "First.Q", "IN2": "b"}},
                        {"name": "First", "type": "R_TRIG", "inputs": {"CLK": "a"}}])
        cols = {b["name"]: b["column"] for b in r["layout"]}
        self.assertEqual((cols["First"], cols["Mid"], cols["Last"]), (0, 1, 2))

    def test_guard_rails(self):
        cases = [([{"name": "T", "type": "TON", "inputs": {"XX": "a"}}], "no input pin"),
                 ([{"name": "A", "type": "AND_BOOL", "inputs": {"IN1": "B.OUT"}},
                   {"name": "B", "type": "AND_BOOL", "inputs": {"IN1": "A.OUT"}}], "loop"),
                 ([{"name": "A", "type": "AND_BOOL", "inputs": {"IN1": "B.Q"}},
                   {"name": "B", "type": "TON", "inputs": {}}], None),
                 ([{"name": "A", "type": "AND_BOOL"}, {"name": "A", "type": "OR_BOOL"}], "duplicate"),
                 ([{"name": "T", "type": "NOPE"}], "Unknown block type"),
                 ([{"name": "T", "type": "TON", "inputs": {"IN": "Z.OUT"}}], None)]
        for blocks, msg in cases:
            if msg:
                with self.assertRaisesRegex(FbdError, msg):
                    self.build(blocks)
        # 'B.Q' where B is a TON is a valid wire; 'Z.OUT' with no block Z is a structured variable
        self.assertEqual(self.build(cases[2][0])["wires"], ["B.Q -> A.IN1"])

    def test_expression_on_pin_is_bound_verbatim(self):
        # IWT binds ST expressions straight onto FBD pins
        r = self.build([{"name": "A", "type": "AND_BOOL",
                         "inputs": {"IN1": "MBTV6.Closed and not WP2.Auto", "IN2": "NOT a AND b"}}])
        self.assertIn('formalParameter="IN1" effectiveParameter="MBTV6.Closed and not WP2.Auto"', r["xml"])
        # 'NOT a AND b' is an expression, not an inverted pin
        self.assertIn('invertedPin="false" formalParameter="IN2" effectiveParameter="NOT a AND b"', r["xml"])
        self.assertEqual(r["declared"], {})

    def test_not_on_single_operand_inverts_pin(self):
        r = self.build([{"name": "A", "type": "AND_BOOL", "inputs": {"IN1": "NOT x", "IN2": "NOT (p OR q)"}}])
        self.assertIn('invertedPin="true" formalParameter="IN1" effectiveParameter="x"', r["xml"])
        self.assertIn('invertedPin="false" formalParameter="IN2" effectiveParameter="NOT (p OR q)"', r["xml"])

    def test_dfb_pin_order_follows_position_pin(self):
        # IWT FC_FSD: in/out 'FSD' sits at PositionPin 5 between the inputs
        from remoteconnect_mcp.fbd_tools import dfb_entry_from_xml
        xml = ('<FBSource nameOfFBType="FC_FSD"><comment>Fixed speed drive</comment><inputParameters>'
               '<variables name="ExtIntrlk" typeName="BOOL"><attribute name="PositionPin" value="1"/></variables>'
               '<variables name="OneDayElapsed" typeName="BOOL"><attribute name="PositionPin" value="6"/></variables>'
               '<variables name="SecInMin" typeName="INT"><attribute name="PositionPin" value="7"/></variables>'
               '</inputParameters><inOutParameters>'
               '<variables name="FSD" typeName="FSD_DDT"><attribute name="PositionPin" value="5"/></variables>'
               '</inOutParameters><outputParameters>'
               '<variables name="Running" typeName="BOOL"><attribute name="PositionPin" value="1"/></variables>'
               '</outputParameters></FBSource>')
        e = dfb_entry_from_xml("FC_FSD", xml)
        spec = ffb_catalog.to_spec(e)
        self.assertEqual([p.name for p in spec.inputs], ["ExtIntrlk", "FSD", "OneDayElapsed", "SecInMin"])
        self.assertEqual([p.name for p in spec.outputs], ["FSD", "Running"])
        self.assertEqual(e["description"], "Fixed speed drive")

    def test_tall_columns_wrap_and_sheet_size_has_margin(self):
        # IWT AIs: 38 independent blocks must not become a 400-row strip
        blocks = [{"name": f"B{i}", "type": "AND_BOOL", "inputs": {"IN1": f"a{i}", "IN2": f"b{i}"}} for i in range(38)]
        r = self.build(blocks)
        self.assertGreater(len({b["x"] for b in r["layout"]}), 1)               # wrapped
        self.assertLessEqual(max(b["x"] + b["width"] for b in r["layout"]), 110)   # never wider than MAX_X
        self.assertIn('<FBDSource nbRows="24" nbColumns="36">', r["xml"])   # always CE's own values
        self.assertLessEqual(max(b["y"] + b["height"] for b in r["layout"]), 190)

    def test_oversized_section_warns(self):
        blocks = [{"name": f"B{i}", "type": "AND_BOOL", "inputs": {"IN1": f"a{i}", "IN2": f"b{i}"}} for i in range(200)]
        r = self.build(blocks)
        self.assertTrue(any("splitting" in w for w in r["warnings"]))

    def test_small_diagram_uses_control_expert_default_sheet(self):
        r = self.build([{"name": "A", "type": "AND_BOOL", "inputs": {"IN1": "x", "IN2": "y"}}])
        self.assertIn('<FBDSource nbRows="24" nbColumns="36">', r["xml"])

    def test_kept_positions_and_new_block_placed_right(self):
        r = self.build([{"name": "A", "type": "AND_BOOL", "pos": [6, 0], "inputs": {"IN1": "x", "IN2": "y"}},
                        {"name": "T", "type": "TON", "pos": [20, 0], "inputs": {"IN": "A.OUT"}},
                        {"name": "New", "type": "R_TRIG", "inputs": {"CLK": "z"}}])
        lay = {b["name"]: b for b in r["layout"]}
        self.assertEqual((lay["A"]["x"], lay["A"]["y"]), (6, 0))
        self.assertEqual((lay["T"]["x"], lay["T"]["y"]), (20, 0))
        self.assertGreater(lay["New"]["x"], 20 + lay["T"]["width"])
        link = re.search(r"(?s)<linkFB>.*?</linkFB>", r["xml"]).group(0)
        self.assertNotIn("gridObjPosition", link)            # same row, target to the right: straight wire

    def test_kept_wire_to_the_left_routes_below(self):
        r = self.build([{"name": "A", "type": "AND_BOOL", "pos": [40, 0], "inputs": {"IN1": "x", "IN2": "y"}},
                        {"name": "T", "type": "TON", "pos": [6, 10], "inputs": {"IN": "A.OUT"}}])
        self.assertEqual(r["xml"].count("<gridObjPosition"), 4)

    def test_bad_pos_rejected(self):
        with self.assertRaisesRegex(FbdError, "pos must be"):
            self.build([{"name": "A", "type": "AND_BOOL", "pos": [3], "inputs": {"IN1": "x", "IN2": "y"}}])

    def test_block_at_maps_build_positions(self):
        r = self.build([{"name": "A", "type": "AND_BOOL", "inputs": {"IN1": "x", "IN2": "y"}}])
        a = r["layout"][0]
        self.assertEqual(block_at(r["layout"], a["x"] + 1, a["y"] + 1), "A")
        self.assertIsNone(block_at(r["layout"], 0, 0))

    def test_catalog_has_core_blocks(self):
        for name in ("TON", "TOF", "TP", "CTU", "SR", "RS", "R_TRIG", "AND_BOOL", "ADD", "GT", "SEL", "INT_TO_REAL"):
            self.assertIsNotNone(ffb_catalog.find(name), name)
        ton = ffb_catalog.find("TON")
        self.assertEqual([p["name"] for p in ton["pins"]], ["IN", "PT", "Q", "ET"])


if __name__ == "__main__":
    unittest.main()
