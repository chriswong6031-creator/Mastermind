"""On-the-spot equity-research PDF generator — deterministic, no AI, ReportLab Platypus.

Renders a saved research paper + live company/fundamental context into a beautifully
formatted, auto-paginating PDF. It is built to be **bug-free across any content length**:

  * Platypus flows every paragraph/table across pages automatically (no manual y-tracking).
  * EVERY text node is XML-escaped before it reaches a Paragraph (a stray '<', '&' or '>'
    in the report would otherwise crash ReportLab's mini-markup parser).
  * The markdown→flowable converter degrades gracefully on malformed input (ragged tables,
    unclosed bold, empty sections, odd bullets) — it never raises.
  * Missing/None fields render as '—' rather than blowing up.

Public API:  build(paper: dict, meta: dict | None) -> bytes   (the PDF, ready to stream)
"""
from __future__ import annotations

import io
import re

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas as _canvas
from reportlab.platypus import (BaseDocTemplate, Flowable, Frame, HRFlowable,
                                ListFlowable, ListItem, PageTemplate, Paragraph,
                                Spacer, Table, TableStyle)

# ── brand palette ─────────────────────────────────────────────────────────────
BLUE = colors.HexColor("#2b6fff")
INDIGO = colors.HexColor("#7c5cff")
INK = colors.HexColor("#0f1729")
BODY = colors.HexColor("#33415c")
MUTED = colors.HexColor("#6b7686")
LINE = colors.HexColor("#e4e8ef")
PANEL = colors.HexColor("#f6f8fc")
UP = colors.HexColor("#13a06a")
DOWN = colors.HexColor("#e0483d")
AMBER = colors.HexColor("#cf8a05")
WHITE = colors.white

_VIAB = {
    "compelling": (UP, "COMPELLING"), "fair": (BLUE, "FAIR VALUE"),
    "rich": (AMBER, "RICH"), "avoid": (DOWN, "AVOID"),
}


# ── tiny formatters (None-safe) ────────────────────────────────────────────────
def _num(v, dp=0):
    try:
        return f"{float(v):,.{dp}f}"
    except (TypeError, ValueError):
        return None


def _money(v, dp=2):
    n = _num(v, dp)
    return f"${n}" if n is not None else None


def _pct(v, dp=1, *, frac=True):
    try:
        x = float(v) * (100.0 if frac else 1.0)
        return f"{x:+.{dp}f}%"
    except (TypeError, ValueError):
        return None


def _mktcap(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
        if abs(x) >= div:
            return f"${x / div:,.1f}{unit}"
    return f"${x:,.0f}"


# ── XML-safe markdown helpers ──────────────────────────────────────────────────
def _esc(s) -> str:
    """Escape for ReportLab's intra-paragraph markup. & MUST be first."""
    return (str("" if s is None else s)
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _inline(escaped: str) -> str:
    """Apply markdown emphasis to ALREADY-escaped text → ReportLab markup. Never raises."""
    try:
        # [text](http…) → a clean blue hyperlink (drops the raw URL clutter)
        s = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
                   r'<link href="\2" color="#2b6fff">\1</link>', escaped)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)              # **bold**
        s = re.sub(r"__(.+?)__", r"<b>\1</b>", s)                  # __bold__
        s = re.sub(r"(?<![\*\w])\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<i>\1</i>", s)  # *italic*
        s = re.sub(r"`(.+?)`", r'<font face="Courier">\1</font>', s)            # `code`
        return s
    except re.error:
        return escaped


def _tags_balanced(markup: str) -> bool:
    """True iff <b>/<i>/<font> tags are properly LIFO-nested (so ReportLab won't choke).
    Malformed source like '**bold *italic**' can produce mis-nested tags — we detect that
    and fall back to plain text rather than crash the whole document."""
    stack = []
    for m in re.finditer(r"</?(b|i|font)\b[^>]*?>", markup):
        tag, name = m.group(0), m.group(1)
        if tag.startswith("</"):
            if not stack or stack[-1] != name:
                return False
            stack.pop()
        elif not tag.endswith("/>"):
            stack.append(name)
    return not stack


def _p(text, style) -> Paragraph:
    """XML-safe Paragraph. Tries markdown emphasis; on any malformed-markup risk falls back
    to plain escaped text, then to empty — so a single bad string can never break the build."""
    esc = _esc(text)
    markup = _inline(esc)
    if not _tags_balanced(markup):
        markup = esc
    try:
        return Paragraph(markup, style)
    except Exception:  # noqa: BLE001 — last-resort: never let one paragraph kill the PDF
        try:
            return Paragraph(esc, style)
        except Exception:  # noqa: BLE001
            return Paragraph("", style)


# ── logo mark (vector, matches the nav SVG) ────────────────────────────────────
def _draw_logo_mark(c, x, y, size):
    """A rounded-square gradient tile with a white 'M' monogram (origin = bottom-left)."""
    r = size * 0.26
    c.saveState()
    try:
        path = c.beginPath()
        path.roundRect(x, y, size, size, r)
        c.clipPath(path, stroke=0, fill=0)
        c.linearGradient(x, y + size, x, y, (BLUE, INDIGO), extend=True)
    except Exception:  # noqa: BLE001 — gradient is cosmetic; never let it break the PDF
        c.setFillColor(INDIGO)
        c.roundRect(x, y, size, size, r, stroke=0, fill=1)
    c.restoreState()
    c.saveState()
    c.setStrokeColor(WHITE)
    c.setLineWidth(max(1.4, size * 0.085))
    c.setLineCap(1)
    c.setLineJoin(1)
    pts = [(0.25, 0.28), (0.25, 0.70), (0.50, 0.45), (0.75, 0.70), (0.75, 0.28)]
    m = c.beginPath()
    m.moveTo(x + pts[0][0] * size, y + pts[0][1] * size)
    for px, py in pts[1:]:
        m.lineTo(x + px * size, y + py * size)
    c.drawPath(m, stroke=1, fill=0)
    c.restoreState()


class LogoBand(Flowable):
    """Top-of-report band: logo tile + wordmark + 'EQUITY RESEARCH' + report date."""

    def __init__(self, width, date_str):
        super().__init__()
        self.width = width
        self.height = 38
        self.date_str = date_str or ""

    def wrap(self, *_):
        return (self.width, self.height)

    def draw(self):
        c = self.canv
        sz = 34
        _draw_logo_mark(c, 0, self.height - sz, sz)
        c.saveState()
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 16)
        c.drawString(sz + 12, self.height - 15, "MASTERMIND")
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 7.5)
        c.drawString(sz + 13, self.height - 27, "EQUITY  RESEARCH")
        if self.date_str:
            c.setFillColor(MUTED)
            c.setFont("Helvetica", 9)
            c.drawRightString(self.width, self.height - 15, self.date_str)
        c.restoreState()


# ── styles ─────────────────────────────────────────────────────────────────────
def _styles():
    return {
        "h1": ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=22, leading=26,
                             textColor=INK, spaceBefore=2, spaceAfter=2),
        "sub": ParagraphStyle("sub", fontName="Helvetica", fontSize=10.5, leading=14,
                              textColor=MUTED, spaceAfter=2),
        "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=13.5, leading=17,
                             textColor=INK, spaceBefore=14, spaceAfter=5),
        "h3": ParagraphStyle("h3", fontName="Helvetica-Bold", fontSize=11, leading=15,
                             textColor=BLUE, spaceBefore=10, spaceAfter=3),
        "body": ParagraphStyle("body", fontName="Helvetica", fontSize=9.7, leading=14.5,
                               textColor=BODY, spaceAfter=7, alignment=TA_LEFT),
        "lead": ParagraphStyle("lead", fontName="Helvetica-Oblique", fontSize=10.5, leading=15,
                               textColor=colors.HexColor("#475569"), spaceAfter=10),
        "sec": ParagraphStyle("sec", fontName="Helvetica-Bold", fontSize=9.5, leading=12,
                              textColor=MUTED, spaceBefore=16, spaceAfter=7),
        "th": ParagraphStyle("th", fontName="Helvetica-Bold", fontSize=8.5, leading=11, textColor=INK),
        "td": ParagraphStyle("td", fontName="Helvetica", fontSize=8.7, leading=12, textColor=BODY),
        "stat_v": ParagraphStyle("stat_v", fontName="Helvetica-Bold", fontSize=17, leading=19,
                                 textColor=INK, alignment=TA_CENTER),
        "stat_k": ParagraphStyle("stat_k", fontName="Helvetica", fontSize=7.2, leading=9,
                                 textColor=MUTED, alignment=TA_CENTER),
        "kv_k": ParagraphStyle("kv_k", fontName="Helvetica", fontSize=8.8, leading=12, textColor=MUTED),
        "kv_v": ParagraphStyle("kv_v", fontName="Helvetica-Bold", fontSize=8.8, leading=12, textColor=INK),
        "risk": ParagraphStyle("risk", fontName="Helvetica", fontSize=9.3, leading=13.5,
                               textColor=BODY, spaceAfter=5),
        "disc": ParagraphStyle("disc", fontName="Helvetica", fontSize=7.6, leading=10, textColor=MUTED),
    }


def _section_label(text, S):
    return _p(text.upper(), S["sec"])


# ── markdown → flowables (robust) ──────────────────────────────────────────────
def _md_table(tbl_lines, S, content_w):
    rows = []
    for ln in tbl_lines:
        body = ln.strip().strip("|")
        cells = [c.strip() for c in body.split("|")]
        # skip the |---|---| separator row
        if cells and all(re.fullmatch(r":?-{1,}:?", c or "-") for c in cells):
            continue
        rows.append(cells)
    rows = [r for r in rows if any(c for c in r)]
    if not rows:
        return None
    ncol = max(len(r) for r in rows)
    ncol = max(1, min(ncol, 8))  # sanity clamp
    data = []
    for ri, r in enumerate(rows):
        r = (r + [""] * ncol)[:ncol]
        st = S["th"] if ri == 0 else S["td"]
        data.append([_p(c, st) for c in r])
    cw = [content_w / ncol] * ncol
    t = Table(data, colWidths=cw, repeatRows=1, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), PANEL),
        ("LINEBELOW", (0, 0), (-1, 0), 0.9, LINE),
        ("LINEBELOW", (0, 1), (-1, -2), 0.4, LINE),
        ("BOX", (0, 0), (-1, -1), 0.5, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return t


def _md_to_flowables(md, S, content_w):
    """Convert report markdown to Platypus flowables. Never raises."""
    flow = []
    para_buf, bullet_buf = [], []

    def flush_para():
        if para_buf:
            txt = " ".join(x.strip() for x in para_buf).strip()
            if txt:
                flow.append(_p(txt, S["body"]))
            para_buf.clear()

    def flush_bullets():
        if bullet_buf:
            items = [ListItem(_p(b, S["body"]), leftIndent=4) for b in bullet_buf if b.strip()]
            if items:
                flow.append(ListFlowable(items, bulletType="bullet", start="•",
                                         leftIndent=14, bulletColor=BLUE,
                                         spaceBefore=2, spaceAfter=7))
            bullet_buf.clear()

    lines = str(md or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i, n = 0, len(lines)
    while i < n:
        line = lines[i].strip()
        # ── table block ──
        if line.startswith("|") and line.count("|") >= 2:
            flush_para(); flush_bullets()
            tbl = []
            while i < n and lines[i].strip().startswith("|"):
                tbl.append(lines[i]); i += 1
            try:
                t = _md_table(tbl, S, content_w)
            except Exception:  # noqa: BLE001
                t = None
            if t is not None:
                flow.append(t)
                flow.append(Spacer(1, 6))
            continue
        # ── blank ──
        if not line:
            flush_para(); flush_bullets(); i += 1; continue
        # ── heading ──
        hm = re.match(r"^(#{1,4})\s+(.*\S)\s*#*$", line)
        if hm:
            flush_para(); flush_bullets()
            lvl = len(hm.group(1))
            flow.append(_p(hm.group(2), S["h2"] if lvl <= 2 else S["h3"]))
            i += 1; continue
        # ── bullet / numbered ──
        bm = re.match(r"^[-*+•]\s+(.*)$", line) or re.match(r"^\d+[.)]\s+(.*)$", line)
        if bm:
            flush_para()
            bullet_buf.append(bm.group(1).strip())
            i += 1; continue
        # ── horizontal rule ──
        if re.fullmatch(r"[-*_]{3,}", line):
            flush_para(); flush_bullets()
            flow.append(HRFlowable(width="100%", thickness=0.5, color=LINE,
                                   spaceBefore=6, spaceAfter=6))
            i += 1; continue
        # ── paragraph text ──
        flush_bullets()
        para_buf.append(line)
        i += 1
    flush_para(); flush_bullets()
    return flow


# ── hero / scorecard / fundamentals ───────────────────────────────────────────
def _stat_card(value, label, S, color=None):
    vstyle = S["stat_v"]
    if color is not None:
        vstyle = ParagraphStyle("v_c", parent=S["stat_v"], textColor=color)
    return [Paragraph(_esc(value), vstyle), Spacer(1, 1), Paragraph(_esc(label), S["stat_k"])]


def _scorecard(paper, meta, S, content_w):
    fv = paper.get("fair_value")
    price = (meta or {}).get("price") or paper.get("price_at_review")
    upside = None
    try:
        if fv is not None and price is not None and float(price) != 0:
            upside = (float(fv) / float(price) - 1.0)
    except (TypeError, ValueError, ZeroDivisionError):
        upside = None
    ci = paper.get("combined")
    ci_color = UP if (ci is not None and ci >= 72) else AMBER if (ci is not None and ci >= 60) else DOWN if ci is not None else INK
    up_color = UP if (upside is not None and upside >= 0) else DOWN if upside is not None else INK
    cells = [
        _stat_card(str(ci) if ci is not None else "—", "CONVICTION INDEX", S, ci_color),
        _stat_card(str(paper.get("engine_score") or "—"), "ENGINE", S),
        _stat_card(str(paper.get("research_score") or "—"), "RESEARCH", S),
        _stat_card(_money(fv, 0) or "—", "FAIR VALUE", S),
        _stat_card(_money(price, 2) or "—", "CURRENT", S),
        _stat_card(_pct(upside) or "—", "UPSIDE TO FV", S, up_color),
    ]
    cw = [content_w / 6.0] * 6
    t = Table([cells], colWidths=cw, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PANEL),
        ("BOX", (0, 0), (-1, -1), 0.6, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 9), ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def _verdict_badge(paper, S):
    viab = str(paper.get("viability") or "").lower()
    color, label = _VIAB.get(viab, (MUTED, (viab or "—").upper()))
    confirmed = paper.get("confirmed")
    rec = "BUY-CONFIRMED" if confirmed else "RESEARCH-ONLY — NOT A CONFIRMED BUY"
    badge = Paragraph(f'<font color="white"><b>&nbsp;{_esc(label)}&nbsp;</b></font>',
                      ParagraphStyle("bd", fontName="Helvetica-Bold", fontSize=10, leading=16,
                                     backColor=color, textColor=WHITE, alignment=TA_LEFT))
    sub = Paragraph(_esc(rec), ParagraphStyle("rec", fontName="Helvetica-Bold", fontSize=8.5,
                    leading=14, textColor=(UP if confirmed else MUTED)))
    t = Table([[badge, sub]], colWidths=[1.7 * inch, None], hAlign="LEFT")
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (0, 0), 0), ("LEFTPADDING", (1, 0), (1, 0), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return t


def _fundamentals(meta, S, content_w):
    m = meta or {}
    pairs = [
        ("Current Price", _money(m.get("price"), 2)),
        ("Market Cap", _mktcap(m.get("market_cap"))),
        ("Sector", m.get("sector")),
        ("Forward P/E", _num(m.get("fwd_pe"), 1)),
        ("Dividend Yield", _pct(m.get("div_yield"), 2) and _pct(m.get("div_yield"), 2).lstrip("+")),
        ("Analyst Target", _money(m.get("analyst_target"), 0)),
        ("Analyst Rating", m.get("rating")),
        ("Revenue Growth", _pct(m.get("rev_growth"))),
        ("Gross Margin", _pct(m.get("gross_margin")) and _pct(m.get("gross_margin")).lstrip("+")),
        ("Net Margin", _pct(m.get("net_margin")) and _pct(m.get("net_margin")).lstrip("+")),
        ("Return on Equity", _pct(m.get("roe")) and _pct(m.get("roe")).lstrip("+")),
        ("Next Earnings", m.get("next_earnings")),
    ]
    pairs = [(k, v) for k, v in pairs if v not in (None, "", "—")]
    if not pairs:
        return None
    # two label/value columns side by side
    half = (len(pairs) + 1) // 2
    left, right = pairs[:half], pairs[half:]
    rows = []
    for r in range(half):
        lk, lv = left[r] if r < len(left) else ("", "")
        rk, rv = right[r] if r < len(right) else ("", "")
        rows.append([Paragraph(_esc(lk), S["kv_k"]), Paragraph(_esc(lv), S["kv_v"]),
                     Paragraph(_esc(rk), S["kv_k"]), Paragraph(_esc(rv), S["kv_v"])])
    cw = [content_w * 0.18, content_w * 0.32, content_w * 0.18, content_w * 0.32]
    t = Table(rows, colWidths=cw, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    return t


# ── footer + page numbers ──────────────────────────────────────────────────────
class NumberedCanvas(_canvas.Canvas):
    """Two-pass canvas so the footer can show 'Page N of M'."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._saved = []

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for st in self._saved:
            self.__dict__.update(st)
            self._draw_footer(total)
            super().showPage()
        super().save()

    def _draw_footer(self, total):
        w, _h = self._pagesize
        self.saveState()
        self.setStrokeColor(LINE)
        self.setLineWidth(0.5)
        self.line(54, 44, w - 54, 44)
        self.setFont("Helvetica", 7.4)
        self.setFillColor(MUTED)
        self.drawString(54, 33, "Mastermind · paper portfolio · accountability, not alpha · "
                                "not investment advice")
        self.drawRightString(w - 54, 33, f"Page {self._pageNumber} of {total}")
        self.restoreState()


# ── public API ─────────────────────────────────────────────────────────────────
def build(paper: dict, meta: dict | None = None) -> bytes:
    """Render a research paper (+ optional company meta) to PDF bytes. Never raises on
    content; only re-raises if ReportLab itself can't write a document at all."""
    paper = paper or {}
    meta = meta or {}
    S = _styles()
    ticker = str(paper.get("ticker") or "—").upper()
    name = meta.get("name") or paper.get("company") or ""
    date_str = _report_date(paper)

    buf = io.BytesIO()
    doc = BaseDocTemplate(
        buf, pagesize=LETTER,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.62 * inch, bottomMargin=0.82 * inch,
        title=f"Mastermind Research — {ticker}", author="Mastermind", subject="Equity Research",
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="main",
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame])])
    cw = doc.width

    story = []
    story.append(LogoBand(cw, date_str))
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=1.1, color=BLUE, spaceAfter=12))

    # title block
    title = ticker + (f" — {name}" if name else "")
    story.append(_p(title, S["h1"]))
    sub_bits = [b for b in [meta.get("sector"), f"Report as of {date_str}" if date_str else None,
                            f"Reviewed {paper.get('confidence')} confidence" if paper.get("confidence") else None]
                if b]
    if sub_bits:
        story.append(_p(" · ".join(str(b) for b in sub_bits), S["sub"]))
    story.append(Spacer(1, 8))
    story.append(_verdict_badge(paper, S))
    story.append(Spacer(1, 10))
    story.append(_scorecard(paper, meta, S, cw))
    story.append(Spacer(1, 4))

    # price assessment lead
    pa = paper.get("price_assessment")
    if pa:
        story.append(Spacer(1, 6))
        story.append(_p(pa, S["lead"]))

    # company description
    desc = meta.get("description")
    if desc:
        story.append(_section_label("Company", S))
        story.append(_p(desc, S["body"]))

    # fundamentals
    fund = _fundamentals(meta, S, cw)
    if fund is not None:
        story.append(_section_label("Fundamental Snapshot", S))
        story.append(fund)

    # summary lead-in
    summary = paper.get("summary")
    if summary:
        story.append(_section_label("Summary", S))
        story.append(_p(summary, S["lead"]))

    # the full report body
    body_md = str(paper.get("report_md") or "")
    if body_md.strip():
        story.append(_section_label("Research Report", S))
        story.extend(_md_to_flowables(body_md, S, cw))

    # key risks
    risks = paper.get("key_risks")
    if isinstance(risks, str):
        risks = [risks]
    elif not isinstance(risks, (list, tuple)):
        risks = []
    risks = [r for r in (risks or []) if str(r).strip()]
    if risks:
        story.append(_section_label("Key Risks", S))
        story.append(ListFlowable(
            [ListItem(_p(r, S["risk"]), leftIndent=4) for r in risks],
            bulletType="bullet", start="•", leftIndent=14, bulletColor=DOWN, spaceAfter=8))

    if len(story) <= 6:  # nothing but the header rendered — still produce a valid page
        story.append(_p("No report content available for this paper.", S["body"]))

    doc.build(story, canvasmaker=NumberedCanvas)
    return buf.getvalue()


def _report_date(paper) -> str:
    raw = paper.get("generated_at") or paper.get("asof") or ""
    s = str(raw)
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if not m:
        return s[:10]
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    mo = months[mo - 1] if 1 <= mo <= 12 else m.group(2)
    return f"{mo} {d}, {y}"
