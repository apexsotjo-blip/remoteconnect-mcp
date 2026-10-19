"""Offline tests for read_st_logic paging/search."""
import unittest

from remoteconnect_mcp.st_tools import StReadError, _st_source, paginate

CODE = "\n".join(f"line {i}" + (" P_01.Auto" if i in (5, 40) else "") for i in range(1, 101))


class StToolsTests(unittest.TestCase):
    def test_paging(self):
        r = paginate(CODE, 1, 30)
        self.assertEqual((r["total_lines"], r["from_line"], r["to_line"], r["next_start_line"]), (100, 1, 30, 31))
        self.assertTrue(r["text"].startswith("  1: line 1"))
        last = paginate(CODE, 91, 30)
        self.assertEqual(last["to_line"], 100)
        self.assertNotIn("next_start_line", last)

    def test_find_with_context(self):
        r = paginate(CODE, find="p_01.auto")
        self.assertEqual(r["matches"], 2)
        self.assertIn(">  5: line 5 P_01.Auto", r["text"])
        self.assertIn("   3: line 3", r["text"])
        self.assertIn("...", r["text"])                      # gap between the two hits

    def test_xml_entities_and_crlf(self):
        xml = "<STSource>IF a &lt; b THEN&#13;\r\n  x := 1;\r\nEND_IF;</STSource>"
        self.assertEqual(_st_source(xml), "IF a < b THEN\n  x := 1;\nEND_IF;")
        with self.assertRaises(StReadError):
            _st_source("<FBDSource/>")


if __name__ == "__main__":
    unittest.main()
