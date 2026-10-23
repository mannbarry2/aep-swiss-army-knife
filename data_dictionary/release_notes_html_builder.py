#!/usr/bin/env python3
"""
data_dictionary/release_notes_html_builder.py  (AEP Swiss Army Knife)
=============================================
Builds RELEASE_NOTES.html, the colour edition of the Data Dictionary release
notes, from RELEASE_NOTES.md. The Markdown stays the source (the workbook links
to it on GitHub); the HTML is regenerated from it, so the two never drift.

Laid out like Adobe Experience League release notes: a release date per
release, area sections with Feature | Description tables, coloured labels
(New / Updated / Fixed / Beta) and coloured TIP / NOTE / IMPORTANT boxes.

Understands only the Markdown RELEASE_NOTES.md uses: #/##/### headings,
paragraphs, '-' lists, pipe tables, '---' rules, '> [!TYPE]' alert boxes, and
inline code, bold, italic, links, <br> and shields.io badge images (which
become coloured labels, so the HTML needs no outside images). Relative links
to repo files point at GitHub, so they work from a local copy of the page.

Stdlib only.

Usage:
    python data_dictionary/release_notes_html_builder.py              # RELEASE_NOTES.md -> RELEASE_NOTES.html
    python data_dictionary/release_notes_html_builder.py --fragment out.html
                                              # body-only page, for hosts that
                                              # supply their own <html>/<head>
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1]
SOURCE = SCRIPT_DIR / "RELEASE_NOTES.md"
TARGET = SCRIPT_DIR / "RELEASE_NOTES.html"
REPO_URL = "https://github.com/mannbarry2/aep-swiss-army-knife/blob/main/"
PAGE_TITLE = "Data Dictionary Release Notes"
AUTHOR = "Barry Mann (barrymann.com)"
# Same author line, but with barrymann.com as a live link for the page footer
# (the <meta name="author"> above stays plain text).
AUTHOR_HTML = 'Barry Mann (<a href="https://barrymann.com">barrymann.com</a>)'

ALERTS = {"NOTE": "Note", "TIP": "Tip", "IMPORTANT": "Important",
          "WARNING": "Warning", "CAUTION": "Caution"}


# ----------------------------------------------------------------------------
# Inline Markdown
# ----------------------------------------------------------------------------
def _href(url: str) -> str:
    """Relative repo paths go to GitHub, so links work from any copy."""
    if url.startswith(("#", "http://", "https://", "mailto:")):
        return url
    return REPO_URL + url


def inline(text: str) -> str:
    s = html.escape(text, quote=False)
    codes: list[str] = []

    def stash(m):
        codes.append(f"<code>{m.group(1)}</code>")
        return f"\x00{len(codes) - 1}\x00"

    s = re.sub(r"`([^`]+)`", stash, s)

    def image(m):
        alt, url = m.group(1), m.group(2)
        if "img.shields.io/badge/" in url:
            return f'<span class="badge badge-{alt.lower()}">{alt}</span>'
        return f'<img src="{html.escape(_href(url))}" alt="{alt}">'

    s = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", image, s)
    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
               lambda m: f'<a href="{html.escape(_href(m.group(2)))}">{m.group(1)}</a>', s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", s)
    s = s.replace("&lt;br&gt;", "<br>")
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], s)


def plain(text: str) -> str:
    """Heading text as GitHub sees it for its anchor (markup stripped)."""
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    return re.sub(r"[`*]", "", t).strip()


def slugify(text: str, seen: dict) -> str:
    """GitHub's heading anchor, so '#october-16-2026' works in both editions."""
    base = re.sub(r"[^\w\- ]", "", plain(text).lower()).replace(" ", "-")
    n = seen.get(base, 0)
    seen[base] = n + 1
    return base if n == 0 else f"{base}-{n}"


# ----------------------------------------------------------------------------
# Blocks
# ----------------------------------------------------------------------------
def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def convert(md: str) -> tuple[str, list[tuple[str, str]]]:
    """Markdown -> (HTML body, [(anchor, title) for every ## heading])."""
    out, toc, seen = [], [], {}
    lines = md.splitlines()
    para: list[str] = []
    i = 0

    def flush():
        if para:
            out.append(f"<p>{inline(' '.join(para))}</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            flush()
            i += 1
        elif stripped == "---":
            flush()
            out.append("<hr>")
            i += 1
        elif m := re.match(r"(#{1,3})\s+(.*)", stripped):
            flush()
            level, text = len(m.group(1)), m.group(2)
            anchor = slugify(text, seen)
            if level == 2:
                toc.append((anchor, plain(text)))
            out.append(f'<h{level} id="{anchor}">{inline(text)}</h{level}>')
            i += 1
        elif stripped.startswith(">"):
            flush()
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip()[1:].strip())
                i += 1
            kind = re.fullmatch(r"\[!(\w+)\]", quote[0]) if quote else None
            if kind and kind.group(1).upper() in ALERTS:
                key = kind.group(1).upper()
                body = inline(" ".join(q for q in quote[1:] if q))
                out.append(f'<div class="alert alert-{key.lower()}">'
                           f'<p class="alert-label">{ALERTS[key]}</p><p>{body}</p></div>')
            else:
                out.append(f"<blockquote><p>{inline(' '.join(quote))}</p></blockquote>")
        elif stripped.startswith("|"):
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            head, body = rows[0], [r for r in rows[1:]
                                   if not all(re.fullmatch(r":?-{3,}:?", c) for c in r)]
            th = "".join(f"<th>{inline(c)}</th>" for c in head)
            trs = "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>"
                          for r in body)
            out.append(f'<div class="table-wrap"><table><thead><tr>{th}</tr></thead>'
                       f"<tbody>{trs}</tbody></table></div>")
        elif re.match(r"[-*]\s+", stripped):
            flush()
            items = []
            while i < len(lines) and lines[i].strip():
                cur = lines[i]
                if re.match(r"\s*[-*]\s+", cur):
                    items.append(re.sub(r"^\s*[-*]\s+", "", cur))
                elif items:
                    items[-1] += " " + cur.strip()
                i += 1
            out.append("<ul>" + "".join(f"<li>{inline(t)}</li>" for t in items) + "</ul>")
        else:
            para.append(stripped)
            i += 1
    flush()
    return "\n".join(out), toc


# ----------------------------------------------------------------------------
# Page
# ----------------------------------------------------------------------------
STYLE = """
/* Layout: a release-notes document page -- sticky "On this page" rail on the
   left at desktop width, one column on a phone. Colour lives in the labels and
   alert boxes; the page itself stays a quiet reading surface. */
:root {
  --bg: #ffffff;
  --surface: #f3f5f8;
  --fg: #1c2330;
  --muted: #5a6474;
  --line: #d8dde4;
  --link: #1a5bb5;
  --new: #2d7d46;      --new-bg: #e5f3e9;
  --updated: #1f5fa8;  --updated-bg: #e3edf9;
  --fixed: #a85400;    --fixed-bg: #fbeedd;
  --beta: #6e3fa3;     --beta-bg: #efe7f8;
  --caution: #b3261e;  --caution-bg: #fbe7e5;
  --font-body: "Source Sans 3", "Segoe UI", "Helvetica Neue", Arial, sans-serif;
  --font-mono: "Source Code Pro", Consolas, "SFMono-Regular", Menlo, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #12161c; --surface: #1b2129; --fg: #e4e8ee; --muted: #9aa4b2;
    --line: #2d3540; --link: #7fb2f0;
    --new: #6fcf8a; --new-bg: #183424;
    --updated: #7fb2f0; --updated-bg: #172a42;
    --fixed: #f0a85a; --fixed-bg: #3a2610;
    --beta: #c3a1ec; --beta-bg: #2c2040;
    --caution: #f08a80; --caution-bg: #3d1b18;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #12161c; --surface: #1b2129; --fg: #e4e8ee; --muted: #9aa4b2;
  --line: #2d3540; --link: #7fb2f0;
  --new: #6fcf8a; --new-bg: #183424;
  --updated: #7fb2f0; --updated-bg: #172a42;
  --fixed: #f0a85a; --fixed-bg: #3a2610;
  --beta: #c3a1ec; --beta-bg: #2c2040;
  --caution: #f08a80; --caution-bg: #3d1b18;
  color-scheme: dark;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.6 var(--font-body);
}
.page {
  max-width: 1180px; margin: 0 auto;
  padding-block: 28px 64px;
  padding-left: max(16px, env(safe-area-inset-left, 0px));
  padding-right: max(16px, env(safe-area-inset-right, 0px));
  display: grid; grid-template-columns: 1fr; gap: 32px;
}
@media (min-width: 960px) {
  .page { grid-template-columns: 220px minmax(0, 1fr); gap: 48px; }
  .toc { position: sticky; top: calc(env(safe-area-inset-top, 0px) + 24px);
         align-self: start; max-height: calc(100vh - 48px); overflow-y: auto; }
}
.doc { min-width: 0; max-width: 860px; }
.crumbs { font-size: 13px; color: var(--muted); letter-spacing: .02em; margin: 0 0 8px; }
.crumbs span + span::before { content: "›"; margin: 0 8px; color: var(--line); }
h1, h2, h3 { line-height: 1.25; text-wrap: balance; margin: 0; }
h1 { font-size: 34px; font-weight: 700; margin-bottom: 12px; }
h2 { font-size: 26px; font-weight: 700; padding-top: 8px; margin: 8px 0 12px; scroll-margin-top: 24px; }
h3 { font-size: 20px; font-weight: 600; margin: 32px 0 8px; scroll-margin-top: 24px; }
p { margin: 0 0 14px; }
a { color: var(--link); text-underline-offset: 2px; }
a:focus-visible { outline: 2px solid var(--link); outline-offset: 2px; border-radius: 2px; }
hr { border: 0; border-top: 1px solid var(--line); margin: 40px 0 28px; }
ul { margin: 0 0 16px; padding-left: 22px; }
li { margin: 4px 0; }
code {
  font: 0.86em/1.4 var(--font-mono); background: var(--surface);
  border: 1px solid var(--line); border-radius: 4px; padding: 1px 5px;
  overflow-wrap: anywhere;
}
.table-wrap { overflow-x: auto; margin: 8px 0 20px; border: 1px solid var(--line); border-radius: 6px; }
table { border-collapse: collapse; width: 100%; font-size: 15px; }
th, td { text-align: left; vertical-align: top; padding: 10px 14px; border-bottom: 1px solid var(--line); }
th { background: var(--surface); font-weight: 600; font-size: 13px; letter-spacing: .03em; text-transform: uppercase; color: var(--muted); }
tbody tr:last-child td { border-bottom: 0; }
td:first-child { font-weight: 600; width: 30%; min-width: 180px; }
td:first-child code { font-weight: 400; color: var(--muted); }
.badge {
  display: inline-block; font-size: 11px; font-weight: 700; line-height: 1;
  letter-spacing: .05em; text-transform: uppercase; padding: 4px 7px 3px;
  border-radius: 999px; vertical-align: 2px; margin: 0 2px; white-space: nowrap;
}
.badge-new { color: var(--new); background: var(--new-bg); }
.badge-updated { color: var(--updated); background: var(--updated-bg); }
.badge-fixed { color: var(--fixed); background: var(--fixed-bg); }
.badge-beta { color: var(--beta); background: var(--beta-bg); }
.alert { border: 1px solid var(--line); border-radius: 6px; padding: 12px 16px; margin: 8px 0 20px; }
.alert p:last-child { margin-bottom: 0; }
.alert-label { font-weight: 700; font-size: 13px; letter-spacing: .05em; text-transform: uppercase; margin-bottom: 4px; }
.alert-tip { background: var(--new-bg); border-color: var(--new); }
.alert-tip .alert-label { color: var(--new); }
.alert-note { background: var(--updated-bg); border-color: var(--updated); }
.alert-note .alert-label { color: var(--updated); }
.alert-important { background: var(--beta-bg); border-color: var(--beta); }
.alert-important .alert-label { color: var(--beta); }
.alert-warning { background: var(--fixed-bg); border-color: var(--fixed); }
.alert-warning .alert-label { color: var(--fixed); }
.alert-caution { background: var(--caution-bg); border-color: var(--caution); }
.alert-caution .alert-label { color: var(--caution); }
.toc-title { font-size: 12px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); margin: 0 0 8px; }
.toc ol { list-style: none; margin: 0; padding: 0; border-left: 2px solid var(--line); }
.toc li { margin: 0; }
.toc a { display: block; padding: 5px 0 5px 14px; color: var(--fg); text-decoration: none; font-size: 15px; }
.toc a:hover { color: var(--link); }
@media (max-width: 959px) {
  .toc ol { display: flex; flex-wrap: wrap; gap: 4px 16px; border-left: 0; }
  .toc a { padding: 2px 0; }
}
.foot { color: var(--muted); font-size: 13px; margin-top: 40px; }
@media (prefers-reduced-motion: no-preference) { html { scroll-behavior: smooth; } }
"""

FONTS = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
         'family=Source+Code+Pro:wght@400;600&family=Source+Sans+3:wght@400;600;700'
         '&display=swap">')


def build(md: str, fragment: bool = False) -> str:
    body, toc = convert(md)
    toc_html = "".join(f'<li><a href="#{a}">{html.escape(t)}</a></li>' for a, t in toc)
    content = (
        f'<div class="page">'
        f'<nav class="toc" aria-label="On this page"><p class="toc-title">On this page</p>'
        f"<ol>{toc_html}</ol></nav>"
        f'<main class="doc"><p class="crumbs"><span>AEP Swiss Army Knife</span>'
        f"<span>Data Dictionary</span><span>Release notes</span></p>\n{body}\n"
        f'<p class="foot">Built from RELEASE_NOTES.md by data_dictionary/release_notes_html_builder.py. '
        f"{AUTHOR_HTML}.</p></main></div>"
    )
    head = (f"<title>{PAGE_TITLE}</title>\n"
            '<meta name="claude:viewport-fit-sides" content="cover">\n'
            f"{FONTS}\n<style>{STYLE}</style>\n")
    if fragment:
        return head + content + "\n"
    return ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            '<meta name="viewport" content="width=device-width, initial-scale=1, '
            'viewport-fit=cover">\n'
            f'<meta name="author" content="{html.escape(AUTHOR)}">\n'
            f"{head}</head>\n<body>\n{content}\n</body>\n</html>\n")


def main() -> None:
    md = SOURCE.read_text(encoding="utf-8")
    args = sys.argv[1:]
    if args and args[0] == "--fragment":
        dest = Path(args[1]) if len(args) > 1 else TARGET.with_name("RELEASE_NOTES.fragment.html")
        dest.write_text(build(md, fragment=True), encoding="utf-8", newline="\n")
    else:
        dest = TARGET
        dest.write_text(build(md), encoding="utf-8", newline="\n")
    print(f"wrote {dest}")


if __name__ == "__main__":
    main()
