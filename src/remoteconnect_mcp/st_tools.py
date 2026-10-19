"""Read ST sections as plain, line-numbered text — paged or searched — instead of exchange XML.

read_section returns the whole exchange XML; IWT's Mirroring_HMI is ~170 KB, far too much for an agent's
context. read_st_logic returns just the code, a page at a time, or only the lines matching a search with
some context. Runs on the bridge's COM worker (call through bridge._run); shared by both servers."""
from __future__ import annotations

import html
import re


class StReadError(ValueError):
    pass


def _st_source(xml: str) -> str:
    m = re.search(r"<STSource>(.*?)</STSource>", xml, re.S)
    if not m:
        raise StReadError("this section has no ST source (use read_section / read_fbd_logic for other languages).")
    # CRLF, escaped CRs (&#13;) and lone CRs all become plain line breaks
    return re.sub(r"\r+\n|\r", "\n", html.unescape(m.group(1)))


def paginate(code: str, start_line: int = 1, max_lines: int = 200, find: str = "", context: int = 2,
             max_hits: int = 50) -> dict:
    """Pure helper (unit-tested): page or search ST text. Line numbers are 1-based."""
    lines = code.split("\n")
    total = len(lines)
    width = len(str(total))
    if find:
        needle = find.lower()
        hits = [i for i, ln in enumerate(lines) if needle in ln.lower()]
        shown: list[str] = []
        last = -10
        for i in hits[:max_hits]:
            lo, hi = max(0, i - context), min(total, i + context + 1)
            if lo > last + 1 and shown:
                shown.append("   ...")
            for j in range(max(lo, last + 1), hi):
                mark = ">" if j == i or j in hits else " "
                shown.append(f"{mark}{j + 1:>{width}}: {lines[j]}")
            last = hi - 1
        return {"total_lines": total, "find": find, "matches": len(hits),
                "shown_matches": min(len(hits), max_hits), "text": "\n".join(shown),
                "hint": "use start_line=<n> to read around a match" if hits else "no match (case-insensitive)"}
    start = max(1, int(start_line))
    end = min(total, start + max(1, int(max_lines)) - 1)
    body = "\n".join(f"{n:>{width}}: {lines[n - 1]}" for n in range(start, end + 1)) if start <= total else ""
    res = {"total_lines": total, "from_line": start, "to_line": end if start <= total else None, "text": body}
    if end < total:
        res["next_start_line"] = end + 1
    return res


def read_st_logic(ce, task: str, section: str, start_line: int, max_lines: int, find: str) -> dict:
    data = ce._do_read_section(task, section)
    if data.get("language") != "ST":
        raise StReadError(f"section '{section}' is {data.get('language')}, not ST.")
    res = paginate(_st_source(data["xml"]), start_line, max_lines, find)
    return {"task": data["task"], "section": data["section"], **res}
