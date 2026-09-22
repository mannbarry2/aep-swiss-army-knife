"""
guardrail_audit_pptx.py  --  one-slide scoreboard deck from a guardrail audit
==========================================================================

Turns the JSON written by guardrail_audit.py (plus the hand-measured baseline
columns and, when present, the previous run from the history file) into a
single 16:9 slide laid out like the original "Scoreboard -- before and after"
one-pager: RAG dot, check, one column per earlier measurement, and today's
measurement in bold, with a headline line underneath.

Usage:
    python guardrail_audit_pptx.py                              # latest prod audit in output/
    python guardrail_audit_pptx.py output/guardrail_audit_prod_2026-09-22.json
    python guardrail_audit_pptx.py --sandbox=prod --date=2026-09-22
    python guardrail_audit_pptx.py --all-columns     # every baseline column + previous run, not just start vs now

Writes output/guardrail_audit_<sandbox>_<date>.pptx next to the JSON. By default
the slide shows two columns only: how things stood at the START of the project
(the first baseline column) and Now -- the column that matters is on the right.
Needs python-pptx (pip install python-pptx).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.util import Inches, Pt

SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCRIPT_DIR / "output"
BASELINE_DIR = SCRIPT_DIR / "baselines"

INK = RGBColor(0x1B, 0x1F, 0x3B)        # near-black navy for text
MUTED = RGBColor(0x6B, 0x72, 0x80)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
ROW_TINT = RGBColor(0xF3, 0xF4, 0xF6)
HEADLINE = RGBColor(0x1B, 0x7F, 0x3B)
RAG = {"red": RGBColor(0xD6, 0x2E, 0x2E), "amber": RGBColor(0xE8, 0xA3, 0x17),
       "green": RGBColor(0x2E, 0x9E, 0x44), "grey": RGBColor(0xB0, 0xB5, 0xBD)}

# Slide geometry (inches, 16:9 wide)
SLIDE_W, SLIDE_H = 13.333, 7.5
MARGIN = 0.5
TABLE_TOP = 1.3
ROW_H = 0.255
HEADLINE_Y = 6.42
FOOTER_Y = 7.08


def latest_json(sandbox: str | None, date: str | None) -> Path:
    files = sorted(OUTPUT_DIR.glob(f"guardrail_audit_{sandbox or '*'}_{date or '*'}.json"))
    files = [f for f in files if "history" not in f.name]
    if not files:
        sys.exit(f"no guardrail_audit_*.json in {OUTPUT_DIR}")
    return files[-1]


def load(json_path: Path, all_columns: bool = False):
    """The deck is deliberately simple: the FIRST baseline column (how things
    stood at the start of the project) and Now. --all-columns adds every
    baseline column plus the previous run from the history file."""
    rep = json.loads(json_path.read_text(encoding="utf-8"))
    sandbox, date = rep["sandbox"], rep["date"]
    cols = []
    try:
        base = json.loads((BASELINE_DIR / f"guardrail_baseline_{sandbox}.json").read_text(encoding="utf-8"))
        cols += [(c["label"], c["values"]) for c in base.get("columns", [])]
    except Exception:  # noqa: BLE001
        pass
    if not all_columns:
        return rep, cols[:1]
    try:
        hist = json.loads((OUTPUT_DIR / f"guardrail_audit_history_{sandbox}.json").read_text(encoding="utf-8"))
        prev = [h for h in hist if h.get("date") < date]
        if prev:
            p = prev[-1]
            cols.append((f"Previous -- {p['date']}", {c["key"]: c for c in p["checks"]}))
    except Exception:  # noqa: BLE001
        pass
    return rep, cols


def short(label: str) -> str:
    """Column header: 'Before -- 4 Aug 2026 (audit v1.1)' -> 'Before — 4 Aug';
    'Previous -- 2026-08-22' stays dated."""
    label = label.replace("--", "—")
    label = re.sub(r"\s*\(.*?\)", "", label)
    return re.sub(r"(\d{1,2} \w{3}) \d{4}", r"\1", label)


def fill_cell(cell, runs, align=PP_ALIGN.LEFT):
    """runs: list of (text, size, color, bold, italic)."""
    cell.text = ""
    tf = cell.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.alignment = align
    for text, size, color, bold, italic in runs:
        r = p.add_run()
        r.text = text
        r.font.size, r.font.bold, r.font.italic = Pt(size), bold, italic
        r.font.name, r.font.color.rgb = "Calibri", color
    cell.vertical_anchor = MSO_ANCHOR.MIDDLE
    cell.margin_left = cell.margin_right = Inches(0.06)
    cell.margin_top = cell.margin_bottom = Inches(0.01)


def build(rep, cols, out: Path):
    checks = rep["checks"]
    sandbox, date = rep["sandbox"], rep["date"]
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(SLIDE_W), Inches(SLIDE_H)
    slide = prs.slides.add_slide(prs.slide_layouts[6])   # blank
    width = SLIDE_W - 2 * MARGIN

    def textbox(y, h, text, size, bold=False, color=INK, italic=False):
        tb = slide.shapes.add_textbox(Inches(MARGIN), Inches(y), Inches(width), Inches(h))
        tf = tb.text_frame
        tf.word_wrap = True
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        r = tf.paragraphs[0].add_run()
        r.text = text
        r.font.size, r.font.bold, r.font.italic = Pt(size), bold, italic
        r.font.name, r.font.color.rgb = "Calibri", color
        return tb

    textbox(0.28, 0.6, "Scoreboard — before and after", 30, bold=True)
    earlier = "  ·  ".join(f"{short(l).split(' — ')[0]} = {l.split('-- ', 1)[-1]}" for l, _ in cols)
    textbox(0.88, 0.36,
            (earlier + "  ·  " if earlier else "") +
            f"Now = re-measured {date} by guardrail_audit.py v{rep.get('version', '')} (read-only API pass on "
            f"{sandbox}). Rows marked query / manual are not measured by the API pass.",
            10, color=MUTED)

    # ---- table -------------------------------------------------------------
    n_rows, n_cols = len(checks) + 1, 3 + len(cols)
    shape = slide.shapes.add_table(n_rows, n_cols, Inches(MARGIN), Inches(TABLE_TOP),
                                   Inches(width), Inches(ROW_H * n_rows))
    tbl = shape.table
    dot_w, check_w, now_factor = 0.32, 2.45, 2.1
    unit = (width - dot_w - check_w) / (len(cols) + now_factor)
    widths = [dot_w, check_w] + [unit] * len(cols) + [unit * now_factor]
    for i, w in enumerate(widths):
        tbl.columns[i].width = Inches(w)
    hdr = ["", "Check"] + [short(l) for l, _ in cols] + [f"Now — {date}"]
    for i, h in enumerate(hdr):
        c = tbl.cell(0, i)
        fill_cell(c, [(h, 9.5, MUTED, True, False)])
        c.fill.solid(); c.fill.fore_color.rgb = WHITE
    for r, chk in enumerate(checks, 1):
        tint = ROW_TINT if r % 2 else WHITE
        for i in range(n_cols):
            tbl.cell(r, i).fill.solid(); tbl.cell(r, i).fill.fore_color.rgb = tint
        fill_cell(tbl.cell(r, 0), [("●", 13, RAG[chk["status"]], False, False)], align=PP_ALIGN.CENTER)
        title = chk["title"].replace(" -- ", " — ")
        runs = [(title, 8.5, INK, True, False)]
        if chk["method"] != "api":
            runs.append((f"  {chk['method']}", 7.5, MUTED, False, True))
        fill_cell(tbl.cell(r, 1), runs)
        for j, (_, vals) in enumerate(cols, 2):
            v = vals.get(chk["key"])
            if isinstance(v, dict):
                st = v.get("status", "grey")
                fill_cell(tbl.cell(r, j), [("● ", 8, RAG[st], False, False),
                                           (v.get("display", ""), 7.5, RAG["red"] if st == "red" else INK, False, False)])
            else:
                fill_cell(tbl.cell(r, j), [("not yet re-measured", 7.5, MUTED, False, True)])
        st = chk["status"]
        fill_cell(tbl.cell(r, n_cols - 1),
                  [(chk["display"], 7.5, RAG["red"] if st == "red" else (MUTED if st == "grey" else INK),
                    st != "grey", st == "grey")])
    for r in range(n_rows):
        tbl.rows[r].height = Inches(ROW_H)

    # ---- headline + footer (fixed slots so a wrapped row can't collide) -------
    reds = [c["title"].replace(" -- ", " — ") for c in checks if c["status"] == "red"]
    ambers = [c["title"].replace(" -- ", " — ") for c in checks if c["status"] == "amber"]
    greens = sum(1 for c in checks if c["status"] == "green")
    measured = sum(1 for c in checks if c["status"] != "grey")
    textbox(HEADLINE_Y, 0.6,
            f"Headline: {greens} of {measured} measured checks green.  Over guardrail: {', '.join(reds) or 'none'}."
            + (f"  Approaching: {', '.join(ambers)}." if ambers else ""),
            10.5, bold=True, color=HEADLINE)
    textbox(FOOTER_Y, 0.3,
            f"AEP Guardrail Audit — before/after one-pager  ·  {sandbox}  ·  generated {date}  ·  "
            f"detail: guardrail_audit_{sandbox}_{date}.json", 8.5, color=MUTED)
    prs.save(out)
    return out


def main():
    args = sys.argv[1:]
    sandbox = next((a.split("=", 1)[1] for a in args if a.startswith("--sandbox=")), None)
    date = next((a.split("=", 1)[1] for a in args if a.startswith("--date=")), None)
    positional = [a for a in args if not a.startswith("-")]
    json_path = Path(positional[0]) if positional else latest_json(sandbox, date)
    rep, cols = load(json_path, all_columns="--all-columns" in args)
    out = json_path.with_suffix(".pptx")
    build(rep, cols, out)
    print(f"deck: {out}")


if __name__ == "__main__":
    main()
