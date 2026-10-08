"""house_xlsx.py -- the one way this repo writes a workbook.

House rules (README, "House rules for generated files"):
  * every report is ONE .xlsx with a stable name, overwritten on each run --
    no CSV alongside, no timestamped copies;
  * Data Dictionary style: STRICTLY CONFIDENTIAL banner, title, italic note,
    header-blue filter row, frozen panes, fixed column widths, uniform row
    heights; a Summary tab first where there is one;
  * the file's author is Barry Mann (barrymann.com), never the library.

Usage:
    from house_xlsx import Book
    book = Book(title="Dataset overlap -- prod", subtitle="what this is ...")
    book.sheet("Cut-list", cols=["Rank", "Profiles", ...], rows=rows,
               widths=[6, 14, ...], note="...", number_formats={2: "#,##0"},
               rag_col=5, tab_colour="7030A0")
    path = book.save(OUTPUT_DIR / "overlap_prod.xlsx")

`rows` are lists in column order. `facts` on the first sheet become a
label/value block above the table. Each sheet gets filters on its header row
and the header row frozen. When openpyxl is missing, save() logs and returns
None; nothing else is written -- there is no CSV fallback by design.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

AUTHOR = "Barry Mann (barrymann.com)"
CONFIDENTIAL = "STRICTLY CONFIDENTIAL"
HEADER_BG = "1F4E78"                   # the Data Dictionary's header blue
SECTION_BG = "DCE6F1"
RAG = {"GREEN": ("C6EFCE", "006100"), "AMBER": ("FFEB9C", "9C5700"),
       "RED": ("FFC7CE", "9C0006"), "GREY": ("EDEDED", "595959")}
ROW_H = 18                             # single-line rows; 30 for wrapped text

logger = logging.getLogger("house_xlsx")


class Book:
    def __init__(self, title: str, subtitle: str = "", generator: str = "", wb=None):
        """`wb` wraps an EXISTING openpyxl workbook (a tool folding its tabs
        into the Data Dictionary); otherwise a new one is started."""
        if wb is not None:
            self.wb, self._first = wb, False
        else:
            try:
                from openpyxl import Workbook
            except ImportError:
                self.wb = None
                return
            self.wb = Workbook()
            self.wb.properties.creator = self.wb.properties.lastModifiedBy = AUTHOR
            self._first = True
        self.title, self.subtitle, self.generator = title, subtitle, generator
        self._tables = []

    # ------------------------------------------------------------------
    def sheet(self, name: str, cols: list, rows: list, widths: list | None = None,
              note: str = "", facts: list | None = None, number_formats: dict | None = None,
              rag_col: int | None = None, rag_cols: tuple = (), wrap_cols: tuple = (),
              row_height: int = ROW_H, tab_colour: str | None = None,
              bold_col: int | None = None, red_when: dict | None = None,
              sections: list | None = None, title: str | None = None):
        """Add one tab. Column numbers are 1-based. `rag_col`/`rag_cols` cells
        holding GREEN / AMBER / RED / GREY get the traffic-light fill.
        `red_when` is {col: value} -> bold red when the cell equals the value.
        `sections` is [(row_index_before_which_to_insert, label), ...] in
        terms of `rows` indices, for divider bands."""
        if self.wb is None:
            return None
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter

        ws = self.wb.active if self._first else self.wb.create_sheet()
        ws.title = _safe_name(name, {s.title.lower() for s in self.wb.worksheets if s is not ws})
        self._first = False
        if tab_colour:
            ws.sheet_properties.tabColor = tab_colour

        head_font = Font(bold=True, color="FFFFFF")
        head_fill = PatternFill("solid", fgColor=HEADER_BG)
        ws["A1"] = CONFIDENTIAL
        ws["A1"].font = Font(bold=True, size=11, color="C00000")
        ws.oddHeader.center.text = f'&"-,Bold"&12&KC00000{CONFIDENTIAL}'
        ws.evenHeader.center.text = ws.oddHeader.center.text
        ws["A2"] = title or self.title
        ws["A2"].font = Font(bold=True, size=14)
        r = 3
        text = note or self.subtitle
        if text:
            ws.cell(r, 1, text).font = Font(italic=True, color="666666")
            ws.cell(r, 1).alignment = Alignment(wrap_text=True, vertical="top")
            span = max(2, min(len(cols), 12))
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=span)
            ws.row_dimensions[r].height = 15 * (1 + len(text) // 140)
            r += 1
        if facts:
            r += 1
            for k, v in facts:
                ws.cell(r, 1, k).font = Font(bold=True)
                ws.cell(r, 2, v).alignment = Alignment(horizontal="left")
                r += 1
        r += 1
        hr = r
        for c, nm in enumerate(cols, 1):
            cell = ws.cell(hr, c, nm)
            cell.font, cell.fill = head_font, head_fill
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        ws.row_dimensions[hr].height = 30
        ws.freeze_panes = ws.cell(hr + 1, 1)

        section_at = {idx: label for idx, label in (sections or [])}
        rr = hr
        for i, row in enumerate(rows):
            if i in section_at:
                rr += 1
                for c in range(1, len(cols) + 1):
                    ws.cell(rr, c).fill = PatternFill("solid", fgColor=SECTION_BG)
                ws.cell(rr, 1, section_at[i]).font = Font(bold=True, color=HEADER_BG)
                ws.row_dimensions[rr].height = 22
            rr += 1
            ws.row_dimensions[rr].height = row_height
            for c, v in enumerate(row, 1):
                cell = ws.cell(rr, c, v)
                cell.alignment = (Alignment(wrap_text=True, vertical="top") if c in wrap_cols
                                  else Alignment(vertical="top"))
                fmt = (number_formats or {}).get(c)
                if fmt and isinstance(v, (int, float)):
                    cell.number_format = fmt
                grade = str(v).upper() if v is not None else ""
                if (c == rag_col or c in rag_cols) and grade in RAG:
                    bg, fg = RAG[grade]
                    cell.fill = PatternFill("solid", fgColor=bg)
                    cell.font = Font(bold=True, color=fg)
                if bold_col == c:
                    cell.font = Font(bold=True)
                if red_when and c in red_when and v == red_when[c]:
                    cell.font = Font(bold=True, color="C00000")
        if rows:
            ws.auto_filter.ref = f"A{hr}:{get_column_letter(len(cols))}{rr}"
        for i, w in enumerate(widths or [], 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        self._tables.append((ws, hr))
        return ws

    # ------------------------------------------------------------------
    def save(self, path: Path):
        """Write the workbook, overwriting any earlier copy. If the file is
        open in Excel, write beside it with a time suffix and say so."""
        if self.wb is None:
            logger.error("openpyxl not installed -- nothing written "
                         "(pip install -r requirements.txt).")
            return None
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.wb.save(path)
        except PermissionError:
            alt = path.with_name(f"{path.stem} ({datetime.now():%H%M%S}){path.suffix}")
            logger.warning(f"{path.name} is open; writing {alt.name} instead.")
            path = alt
            self.wb.save(path)
        return path


def _safe_name(name: str, used: set) -> str:
    import re
    s = re.sub(r"[\[\]\:\*\?\/\\]", "-", name).strip()[:31] or "sheet"
    base, i = s, 2
    while s.lower() in used:
        suffix = f" ({i})"
        s = base[:31 - len(suffix)] + suffix
        i += 1
    return s


def stable_name(stem: str) -> str:
    """'Dataset Census - prod' -> a filename-safe stem, no timestamp."""
    import re
    return re.sub(r"[^0-9A-Za-z._ -]+", "-", stem).strip("- ") or "report"
