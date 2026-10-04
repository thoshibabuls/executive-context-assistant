"""Shared drawing kit for the presentation figures (Figure 1 and Figure 2).

Each figure is laid out on a fixed 1920 x 1080 canvas as absolutely positioned HTML boxes, with
arrows drawn in one SVG layer underneath. ``render.mjs`` turns the page into a 3840 x 2160 PNG
and a vector PDF, and fails if any text overflows its box or a label touches a box or a line.

One visual vocabulary for both figures, so a reader learns it once:

- white box, grey outline ............ a step or module that is plain code
- lavender box + purple ``AI-nn`` tag . a step that calls a model (role from config/models.yaml)
- dark-outlined pill ................. an input or output of the whole figure
- blue box with a cylinder ........... a data store
- dashed outline ..................... a service we call but do not run
- labelled arrow ..................... the data that moves; an unlabelled short arrow is "next step"
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field

W, H = 1920, 1080

INK = "#0F1B2D"
BODY = "#3A4658"
MUTED = "#5A6676"
RULE = "#B9C3CF"
LINE = "#5A6676"
PANEL = "#F4F6F9"
BLUE = "#0F6CBD"
STORE_BG = "#EAF2FB"
AI = "#5C2D91"
AI_BG = "#F6F1FC"
AI_RULE = "#8A63C0"

SANS = "'IBM Plex Sans', 'Segoe UI', Arial, sans-serif"
MONO = "'IBM Plex Mono', Consolas, 'Courier New', monospace"
FONTS = ("https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700"
         "&family=IBM+Plex+Mono:wght@500;600&display=swap")

CSS = f"""
*{{margin:0;padding:0;box-sizing:border-box}}
body{{width:{W}px;height:{H}px;background:#FFFFFF;font-family:{SANS};color:{INK};
  -webkit-font-smoothing:antialiased;text-rendering:geometricPrecision}}
#fig{{position:relative;width:{W}px;height:{H}px;overflow:hidden}}
.abs{{position:absolute}}
.kicker{{font:600 14px/1 {MONO};letter-spacing:.08em;text-transform:uppercase;color:{BLUE}}}
.title{{font-weight:600;font-size:34px;line-height:1.15;letter-spacing:-.01em;color:{INK}}}
.sub{{font-size:16px;line-height:1.35;color:{MUTED}}}
.panel{{background:{PANEL};border-radius:14px}}
.plabel{{font:600 15px/1.3 {MONO};letter-spacing:.06em;text-transform:uppercase;color:{MUTED}}}
.plabel b{{color:{INK};font-weight:700;margin-right:6px}}
.box{{background:#FFFFFF;border:1.5px solid {RULE};border-radius:10px;padding:9px 12px 10px;
  display:flex;flex-direction:column;gap:3px;overflow:hidden}}
.box.ai{{background:{AI_BG};border-color:{AI_RULE}}}
.box.io{{border:2px solid {INK};border-radius:24px;padding:10px 16px}}
.box.store{{background:{STORE_BG};border-color:{BLUE}}}
.box.ext{{border:1.5px dashed #8A94A3;background:#FFFFFF}}
.box.group{{background:transparent;border:1.5px solid {RULE};border-radius:14px}}
.t{{font-size:23px;font-weight:600;line-height:1.2;color:{INK};white-space:nowrap}}
.k{{font:500 13px/1.3 {MONO};color:{MUTED};white-space:nowrap}}
.ai .k{{color:{AI}}}
.d{{font-size:19px;line-height:1.3;color:{BODY}}}
.steps{{font-size:19px;line-height:1.3;color:{BODY};display:grid;gap:7px}}
.steps span.n{{font:600 14px/1.3 {MONO};color:{BLUE};display:inline-block;width:22px}}
.tag{{position:absolute;font:600 14px/24px {MONO};height:24px;padding:0 9px;border-radius:6px;
  background:{AI};color:#FFFFFF;letter-spacing:.02em;white-space:nowrap}}
.lbl{{position:absolute;font-size:17px;line-height:1.25;color:{BODY}}}
.chip{{font-size:17px;line-height:1;padding:7px 11px;border-radius:7px;background:#FFFFFF;
  border:1px solid {RULE};color:{INK};white-space:nowrap}}
.cap{{font-size:15px;line-height:1.42;color:{BODY}}}
.cap b{{color:{INK}}}
.legend{{display:flex;align-items:center;gap:22px;font-size:14px;color:{BODY};white-space:nowrap}}
.legend i{{display:inline-block;vertical-align:middle;margin-right:7px}}
"""


def esc(text: str) -> str:
    return html.escape(text, quote=False)


@dataclass
class Figure:
    """A 1920 x 1080 figure: HTML boxes on top, one SVG arrow layer underneath."""

    parts: list[str] = field(default_factory=list)
    svg: list[str] = field(default_factory=list)

    # ---- text and containers -------------------------------------------------------------
    def raw(self, markup: str) -> None:
        self.parts.append(markup)

    def text(self, x: float, y: float, w: float, markup: str, cls: str, extra: str = "") -> None:
        self.parts.append(f'<div class="abs {cls}" style="left:{x}px;top:{y}px;width:{w}px;{extra}">'
                          f"{markup}</div>")

    def panel(self, x: float, y: float, w: float, h: float, letter: str, name: str) -> None:
        self.parts.append(f'<div class="abs panel" style="left:{x}px;top:{y}px;width:{w}px;height:{h}px">'
                          f"</div>")
        self.text(x + 16, y + 11, w - 32, f"<b>{esc(letter)}</b>{esc(name)}", "plabel")

    def box(self, x: float, y: float, w: float, h: float, title: str, kind_line: str = "",
            detail: str = "", cls: str = "", tags: tuple[str, ...] = (), body: str = "") -> None:
        inner = f'<div class="t">{esc(title)}</div>'
        if kind_line:
            inner += f'<div class="k">{esc(kind_line)}</div>'
        if detail:
            inner += f'<div class="d">{detail}</div>'
        inner += body
        self.parts.append(f'<div class="abs box chk {cls}" style="left:{x}px;top:{y}px;width:{w}px;'
                          f'height:{h}px">{inner}</div>')
        # AI role tags sit on the top-right corner, straddling the border, so the title stays clear.
        right = x + w - 10
        for tag in reversed(tags):
            width = 14 * 0.6 * len(tag) + 18
            self.parts.append(f'<div class="tag" style="left:{right - width:.0f}px;top:{y - 12}px">'
                              f"{esc(tag)}</div>")
            right -= width + 6

    def label(self, x: float, y: float, w: float, text: str, align: str = "left") -> None:
        self.parts.append(f'<div class="lbl chkl" style="left:{x}px;top:{y}px;width:{w}px;'
                          f'text-align:{align}">{text}</div>')

    # ---- arrows --------------------------------------------------------------------------
    def arrow(self, *pts: tuple[float, float], both: bool = False, dashed: bool = False,
              color: str = LINE, width: float = 2) -> None:
        d = "M " + " L ".join(f"{x} {y}" for x, y in pts)
        dash = ' stroke-dasharray="7 6"' if dashed else ""
        start = ' marker-start="url(#hs)"' if both else ""
        self.svg.append(f'<path class="seg" d="{d}" fill="none" stroke="{color}" stroke-width="{width}" '
                        f'stroke-linejoin="round" stroke-linecap="round"{dash}{start} marker-end="url(#he)"/>')

    def cylinder(self, x: float, y: float, w: float = 34, h: float = 42, color: str = BLUE) -> None:
        r = w / 2
        ry = 6
        self.svg_top.append(
            f'<g fill="#FFFFFF" stroke="{color}" stroke-width="2">'
            f'<path d="M {x} {y + ry} L {x} {y + h - ry} A {r} {ry} 0 0 0 {x + w} {y + h - ry} '
            f'L {x + w} {y + ry}"/>'
            f'<ellipse cx="{x + r}" cy="{y + ry}" rx="{r}" ry="{ry}"/>'
            f'<path d="M {x} {y + h / 2} A {r} {ry} 0 0 0 {x + w} {y + h / 2}" fill="none"/></g>')

    svg_top: list[str] = field(default_factory=list)

    # ---- output --------------------------------------------------------------------------
    def html(self, title: str) -> str:
        defs = (f'<defs><marker id="he" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="9" '
                f'markerHeight="9" orient="auto-start-reverse" markerUnits="userSpaceOnUse">'
                f'<path d="M 0 0.8 L 9.5 5 L 0 9.2 Z" fill="{LINE}"/></marker>'
                f'<marker id="hs" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="9" markerHeight="9" '
                f'orient="auto-start-reverse" markerUnits="userSpaceOnUse">'
                f'<path d="M 0 0.8 L 9.5 5 L 0 9.2 Z" fill="{LINE}"/></marker></defs>')
        under = (f'<svg class="abs" style="left:0;top:0" width="{W}" height="{H}" viewBox="0 0 {W} {H}">'
                 + defs + "".join(self.svg) + "</svg>")
        over = (f'<svg class="abs" style="left:0;top:0;pointer-events:none" width="{W}" height="{H}" '
                f'viewBox="0 0 {W} {H}">' + "".join(self.svg_top) + "</svg>")
        # Panels paint first, then arrows, then boxes and labels, then icons.
        panels = [p for p in self.parts if 'class="abs panel"' in p]
        rest = [p for p in self.parts if 'class="abs panel"' not in p]
        return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
                f"<title>{esc(title)}</title>"
                f'<link rel="stylesheet" href="{FONTS}"><style>{CSS}</style></head>'
                f'<body><div id="fig">{"".join(panels)}{under}{"".join(rest)}{over}</div></body></html>')
