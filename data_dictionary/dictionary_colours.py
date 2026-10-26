"""
data_dictionary/dictionary_colours.py  (AEP Swiss Army Knife)
=======================================
The ONE colour scheme of the Data Dictionary workbook and of the Explorer
drawn from it, so a colour means the same thing in both. Hex without '#',
as openpyxl takes it; the Explorer turns these into CSS variables.

A pill is (fill, text): a light fill with bold dark text of the same hue --
a cell in the workbook, a rounded badge in the Explorer.
"""

HEADER = "1F4E78"        # header rows, section titles
LINK = "0563C1"
MUTED = "666666"
WARN = "C00000"          # STRICTLY CONFIDENTIAL, warnings

# Schema class: (dark = data in Profile, light = not in Profile). Tab colours.
KIND = {"profile": ("7030A0", "C9B8E8"),
        "event": ("0070C0", "9DC3E6"),
        "other": ("548235", "C5E0B4")}

# Traffic lights: (fill, text).
RAG = {"GREEN": ("C6EFCE", "006100"),
       "AMBER": ("FFEB9C", "9C5700"),
       "RED": ("FFC7CE", "9C0006"),
       "GREY": ("EDEDED", "595959")}

# Pills for the kinds of thing, light fill + dark text of the same hue.
PILL = {"profile": ("E4DFEC", "7030A0"),
        "event": ("DDEBF7", "0070C0"),
        "other": ("E2EFDA", "548235"),
        "audience": ("FBE5D6", "C55A11"),
        "journey": ("D9F0EE", "0E6E73"),
        "destination": ("F8DCEA", "A23B72"),
        "source": ("FFF2CC", "7F6000"),
        "neutral": ("EDEDED", "404040")}

# Column bands above the Field Index / schema tab headers: (fill, text).
BANDS = {"field": ("D9E1F2", "1F4E78"),
         "data": ("E2EFDA", "375623"),
         "history": ("E4DFEC", "6E3FA3"),
         "sql": ("FBE5D6", "C55A11")}

# Colour scales, (value, colour) from low to high.
COVERAGE_SCALE = [(0, "F8CBAD"), (50, "FFE699"), (100, "A9D08E")]
MONTHS_SCALE = [(0, "FFFFFF"), (24, "A9D08E")]
DAYS_SCALE = [(0, "FFFFFF"), (90, "C9B8E8")]


def scale_colour(scale, value) -> str:
    """The colour a value gets on a scale, as Excel's colour-scale rule draws it."""
    pts = sorted(scale)
    if value <= pts[0][0]:
        return pts[0][1]
    for (v0, c0), (v1, c1) in zip(pts, pts[1:]):
        if value <= v1:
            f = (value - v0) / (v1 - v0) if v1 != v0 else 1
            return "".join(f"{round(int(c0[i:i + 2], 16) + f * (int(c1[i:i + 2], 16) - int(c0[i:i + 2], 16))):02X}"
                           for i in (0, 2, 4))
    return pts[-1][1]


# ---- Which pill a cell gets, by its column's header (workbook and Explorer alike)
LIVE_JOURNEY = {"deployed", "live", "closed"}
TRYING = {"draft", "dry run", "testing"}


def _yes_no(v):
    return RAG["GREEN"] if str(v).strip().lower() == "yes" else RAG["GREY"]


def _kind(v):
    s = str(v or "").lower()
    return PILL["profile"] if s.startswith("profile") else PILL["event"] if s.startswith("event") else PILL["other"]


def _status(v):
    s = str(v or "").strip().lower()
    return RAG["GREEN"] if s in LIVE_JOURNEY else RAG["AMBER"] if s in TRYING else RAG["GREY"]


def _lifecycle(v):
    return RAG["GREEN"] if str(v or "").lower() == "published" else RAG["GREY"]


def _profile(v):
    s = str(v or "")
    return PILL["profile"] if s.startswith(("Profile", "snapshot")) else None


PILL_COLUMNS = {
    "Live now": _yes_no, "Into Profile": _yes_no, "In Profile": _yes_no,
    "Kind": _kind, "Status": _status, "Lifecycle": _lifecycle, "Profile": _profile,
    "How it comes in": lambda v: PILL["source"],
    "Destination": lambda v: PILL["destination"],
    "Evaluation": lambda v: PILL["neutral"],
    "How": lambda v: PILL["journey"],
}


def pill_for(header: str, value):
    """(fill, text) for a cell, or None when its column carries no pill."""
    f = PILL_COLUMNS.get(header)
    return f(value) if f and value not in (None, "") else None
