r"""LaTeX -> Unicode text converter (self-contained, no dependency).

Covers the constructs commonly found in zhihu equation images:

  - Greek letters: \alpha -> α, \Delta -> Δ ...
  - superscripts/subscripts: x^2 -> x², a_{ij} -> aᵢⱼ (unicode where
    available, ^(...)/_(...) fallback otherwise)
  - \frac{a}{b} -> a/b, \sqrt{x} -> √(x)
  - arrows, relations, operators, delimiters
  - \mathcal/\mathbb/\mathrm/\rm/\text{...} decorations
  - \sum/\prod/\int limits

This module is used as a *fallback*; when the `utftex` CLI (unicode-art
equation renderer) is installed, `utftex_render()` prefers its output.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from functools import lru_cache

from rich.cells import cell_len

# --------------------------------------------------------------------------
# unicode maps
# --------------------------------------------------------------------------

GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ",
    "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ", "eta": "η",
    "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "omicron": "ο",
    "pi": "π", "varpi": "ϖ", "rho": "ρ", "varrho": "ϱ", "sigma": "σ",
    "varsigma": "ς", "tau": "τ", "upsilon": "υ", "phi": "ϕ",
    "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ",
    "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ",
    "Psi": "Ψ", "Omega": "Ω",
}

SUPER = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵",
    "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹", "+": "⁺", "-": "⁻",
    "=": "⁼", "(": "⁽", ")": "⁾", "n": "ⁿ", "i": "ⁱ", "a": "ᵃ",
    "b": "ᵇ", "c": "ᶜ", "d": "ᵈ", "e": "ᵉ", "f": "ᶠ", "g": "ᵍ",
    "h": "ʰ", "j": "ʲ", "k": "ᵏ", "l": "ˡ", "m": "ᵐ", "o": "ᵒ",
    "p": "ᵖ", "r": "ʳ", "s": "ˢ", "t": "ᵗ", "u": "ᵘ", "v": "ᵛ",
    "w": "ʷ", "x": "ˣ", "y": "ʸ", "z": "ᶻ",
}

SUB = {
    "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅",
    "6": "₆", "7": "₇", "8": "₈", "9": "₉", "+": "₊", "-": "₋",
    "=": "₌", "(": "₍", ")": "₎", "a": "ₐ", "e": "ₑ", "h": "ₕ",
    "i": "ᵢ", "j": "ⱼ", "k": "ₖ", "l": "ₗ", "m": "ₘ", "n": "ₙ",
    "o": "ₒ", "p": "ₚ", "r": "ᵣ", "s": "ₛ", "t": "ₜ", "u": "ᵤ",
    "v": "ᵥ", "x": "ₓ",
}

OPERATORS = {
    "times": "×", "cdot": "·", "div": "÷", "pm": "±", "mp": "∓",
    "ast": "∗", "star": "⋆", "circ": "∘", "bullet": "∙", "cap": "∩",
    "cup": "∪", "vee": "∨", "wedge": "∧", "oplus": "⊕", "ominus": "⊖",
    "otimes": "⊗", "oslash": "⊘", "odot": "⊙", "dagger": "†",
    "ddagger": "‡", "amalg": "⨿",
}

RELATIONS = {
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠",
    "ne": "≠", "sim": "∼", "simeq": "≃", "approx": "≈", "cong": "≅",
    "equiv": "≡", "propto": "∝", "ll": "≪", "gg": "≫", "prec": "≺",
    "succ": "≻", "preceq": "⪯", "succeq": "⪰", "subset": "⊂",
    "supset": "⊃", "subseteq": "⊆", "supseteq": "⊇", "in": "∈",
    "notin": "∉", "ni": "∋", "vdash": "⊢", "dashv": "⊣",
    "perp": "⊥", "mid": "∣", "parallel": "∥", "models": "⊨",
    "asymp": "≍", "bowtie": "⋈",
}

ARROWS = {
    "to": "→", "rightarrow": "→", "gets": "←", "leftarrow": "←",
    "leftrightarrow": "↔", "Rightarrow": "⇒", "Leftarrow": "⇐",
    "Leftrightarrow": "⇔", "uparrow": "↑", "downarrow": "↓",
    "updownarrow": "↕", "mapsto": "↦", "longrightarrow": "→",
    "longleftarrow": "←", "Longrightarrow": "⇒", "Longleftarrow": "⇐",
    "nearrow": "↗", "searrow": "↘", "nwarrow": "↖", "swarrow": "↙",
    "rightleftharpoons": "⇌",
}

SYMBOLS = {
    "infty": "∞", "partial": "∂", "nabla": "∇", "hbar": "ℏ",
    "ell": "ℓ", "forall": "∀", "exists": "∃", "nexists": "∄",
    "emptyset": "∅", "varnothing": "∅", "neg": "¬", "lnot": "¬",
    "top": "⊤", "bot": "⊥", "angle": "∠", "measuredangle": "∡",
    "triangle": "△", "Box": "□", "square": "□", "blacksquare": "■",
    "aleph": "ℵ", "beth": "ℶ", "prime": "′", "backprime": "‵",
    "degree": "°", "mho": "℧", "Re": "ℜ", "Im": "ℑ",
    "wp": "℘", "S": "§", "P": "¶", "clubsuit": "♣",
    "diamondsuit": "♢", "heartsuit": "♡", "spadesuit": "♠",
    "checkmark": "✓", "flat": "♭", "natural": "♮", "sharp": "♯",
    "cdots": "⋯", "ldots": "…", "dots": "…", "vdots": "⋮",
    "ddots": "⋱", "colon": ":", "dotsb": "⋯",
}

LARGE_OPS = {"sum": "∑", "prod": "∏", "coprod": "∐", "int": "∫",
             "iint": "∬", "iiint": "∭", "oint": "∮", "bigcap": "⋂",
             "bigcup": "⋃", "bigvee": "⋁", "bigwedge": "⋀", "bigoplus": "⨁",
             "bigotimes": "⨂", "bigodot": "⨀", "lim": "lim", "max": "max",
             "min": "min", "sup": "sup", "inf": "inf", "log": "log",
             "ln": "ln", "exp": "exp", "sin": "sin", "cos": "cos",
             "tan": "tan", "cot": "cot", "sec": "sec", "csc": "csc",
             "sinh": "sinh", "cosh": "cosh", "tanh": "tanh", "coth": "coth",
             "arcsin": "arcsin", "arccos": "arccos", "arctan": "arctan",
             "det": "det", "dim": "dim", "ker": "ker", "deg": "deg",
             "mod": "mod", "gcd": "gcd", "Pr": "Pr", "arg": "arg",
             "rank": "rank", "limsup": "limsup", "liminf": "liminf",
}

# combining decoration marks: \bar{x} -> x̄
DECOR = {
    "bar": "\u0304", "hat": "\u0302", "tilde": "\u0303",
    "vec": "\u20d7", "dot": "\u0307", "ddot": "\u0308",
}

DELIMITERS = {
    "left": "", "right": "", "big": "", "Big": "", "bigg": "",
    "Bigg": "", "bigl": "", "bigr": "", "Bigl": "", "Bigr": "",
    "biggl": "", "biggr": "", "Biggl": "", "Biggr": "",
    "lvert": "|", "rvert": "|", "lVert": "‖", "rVert": "‖",
    "langle": "⟨", "rangle": "⟩", "lceil": "⌈", "rceil": "⌉",
    "lfloor": "⌊", "rfloor": "⌋", "backslash": "\\", "vert": "|",
    "Vert": "‖", "lbrack": "[", "rbrack": "]", "lbrace": "{",
    "rbrace": "}", "arrowvert": "|", "Arrowvert": "‖",
    "uparrow": "↑", "downarrow": "↓",
}

MISC = {
    "qquad": "  ", "quad": "  ", " ": " ", "phantom": "",
    "hspace": "", "vspace": "", "nonumber": "", "label": "",
    "displaystyle": "", "textstyle": "", "tiny": "", "small": "",
    "normalsize": "", "large": "", "Large": "", "LARGE": "",
    "huge": "", "Huge": "", "mathbf": "", "bm": "", "boldsymbol": "",
    "mathit": "", "mathsf": "", "mathtt": "", "boldsymbol": "",
    "boldsymbol": "", "boldsymbol": "",
    "qquad": "  ",
}

_CMD_RE = re.compile(r"\\([a-zA-Z]+)\*?")
_GROUP_RE = re.compile(r"\{([^{}]*)\}")
_FRAC_RE = re.compile(r"\\frac\s*(\{[^{}]*\}|\S)\s*(\{[^{}]*\}|\S)")
_SQRT_RE = re.compile(r"\\sqrt(?:\[[^\]]*\])?\s*(\{[^{}]*\}|\S)")
_SUM_RE = re.compile(r"\\(sum|prod|int|iint|oint|bigcup|bigcap)\s*(?:\^(\{[^{}]*\}|\S))?\s*(?:_(\{[^{}]*\}|\S))?")
_TEXT_RE = re.compile(r"\\text(?:\{[^{}]*\})?")
_RM_RE = re.compile(r"\\rm\b")
_MATHCAL_RE = re.compile(r"\\mathcal\{([^{}]*)\}")
_MATHBB_RE = re.compile(r"\\mathbb\{([^{}]*)\}")
_LEFT_RE = re.compile(r"\\left\s*([.|]|\\[a-zA-Z]+|.)")
_RIGHT_RE = re.compile(r"\\right\s*([.|]|\\[a-zA-Z]+|.)")

_UTF_TEX = shutil.which("utftex")


def utftex_available() -> bool:
    return _UTF_TEX is not None


@lru_cache(maxsize=512)
def utftex_render(latex: str) -> str | None:
    """Render LaTeX with the `utftex` CLI (unicode-art, possibly multi-line).

    Returns the normalized output, or None if the tool is missing/failed.
    """
    if _UTF_TEX is None:
        return None
    try:
        r = subprocess.run(
            [_UTF_TEX],
            input=latex,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode != 0:
            return None
        lines = [ln.rstrip() for ln in r.stdout.splitlines()]
        lines = [re.sub(r"\[[0-9.]+(pt|cm|mm|ex|em)\]", "", ln) for ln in lines]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        if not lines:
            return None
        # Textual centers every visual line independently. utftex's output is
        # one piece of fixed-width Unicode art, so differing line widths make
        # braces, fraction bars and case rows drift apart when centered. Pad
        # every row to the same terminal-cell width to center the block as a
        # whole while preserving utftex's internal alignment.
        if len(lines) > 1:
            width = max(cell_len(ln) for ln in lines)
            lines = [ln + " " * (width - cell_len(ln)) for ln in lines]
        return "\n".join(lines)
    except Exception:
        return None


def render_math(latex: str, inline: bool) -> str:
    """Render a LaTeX formula. utftex CLI is authoritative when installed.

    When utftex is missing or fails, the built-in ``mathtex`` unicode
    converter is used as a silent fallback.  Inline formulas must stay
    single-line (they live inside a paragraph), so multi-line utftex
    output also falls back to the flat converter.
    """
    out = utftex_render(latex)
    if out is not None and not (inline and "\n" in out):
        return out
    try:
        return latex_to_unicode(latex)
    except Exception:
        return latex


def _to_script(chars: str, table: dict[str, str]) -> str:
    out = []
    for ch in chars:
        out.append(table.get(ch, ""))
    return "".join(out)


def latex_to_unicode(src: str) -> str:
    """Convert a LaTeX snippet to readable unicode text (fallback converter).

    Used when the utftex CLI is unavailable, or when an inline formula must
    stay single-line.  Display formulas prefer utftex's multi-line output.
    """
    s = src.strip()
    s = s.replace("\\left\\{", "{").replace("\\right\\}", "}")
    s = s.replace("\\left(", "(").replace("\\right)", ")")
    s = s.replace("\\left[", "[").replace("\\right]", "]")
    s = s.replace("\\langle", "⟨").replace("\\rangle", "⟩")
    s = s.replace("\\lvert", "|").replace("\\rvert", "|")
    s = s.replace("\\ldots", "…").replace("\\cdots", "⋯")
    s = s.replace("\\,", " ").replace("\\;", " ").replace("\\!", "")
    s = s.replace("\\ ", " ")
    s = re.sub(r"\\left\\s*\\.", "", s)
    s = re.sub(r"\\right\\s*\\.", "", s)
    out = _convert(s)
    out = re.sub(r"\[[0-9.]+(pt|cm|mm|ex|em)\]", "", out)
    out = re.sub(r"\\\\", "", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def _strip_braces(tok: str) -> str:
    if tok.startswith("{") and tok.endswith("}"):
        return tok[1:-1]
    return tok


def _conv_frac(num: str, den: str) -> str:
    n = _strip_braces(num).strip()
    d = _strip_braces(den).strip()
    return f"{n}/{d}"


def _script(txt: str, table: dict[str, str]) -> str:
    txt = txt.strip()
    if not txt:
        return ""
    unicode_all = _to_script(txt, table)
    if len(txt) == len(unicode_all) and all(table.get(c) for c in txt):
        return unicode_all
    return f"({txt})" if table is SUPER else f"({txt})"


# --------------------------------------------------------------------------
# recursive parser (balanced braces) used as the fallback converter
# --------------------------------------------------------------------------

_MATH_CMDS = {
    **GREEK, **OPERATORS, **RELATIONS, **ARROWS, **SYMBOLS, **LARGE_OPS,
}


def _read_group(s: str, i: int) -> tuple[str, int]:
    """Read a group starting at s[i]: '{...}' balanced, or a single char.

    Returns (inner_text, next_index).
    """
    if i >= len(s):
        return "", i
    if s[i] == "{":
        depth = 1
        j = i + 1
        while j < len(s) and depth:
            if s[j] == "{":
                depth += 1
            elif s[j] == "}":
                depth -= 1
            j += 1
        return s[i + 1 : j - 1], j
    return s[i], i + 1


def _convert(src: str) -> str:
    out: list[str] = []
    i = 0
    n = len(src)
    while i < n:
        ch = src[i]
        if ch == "\\" and i + 1 < n:
            m = re.match(r"\\([a-zA-Z]+)\*?", src[i:])
            if m:
                name = m.group(1)
                i += m.end()
                if name in ("frac", "dfrac", "tfrac"):
                    num, i = _read_group(src, i)
                    den, i = _read_group(src, i)
                    out.append(f"{_convert(num)}/{_convert(den)}")
                elif name == "sqrt":
                    if i < n and src[i] == "[":
                        j = src.find("]", i)
                        i = j + 1 if j != -1 else i + 1
                    body, i = _read_group(src, i)
                    out.append(f"√({_convert(body)})")
                elif name in DECOR:
                    body, i = _read_group(src, i)
                    out.append(_convert(body) + DECOR[name])
                elif name == "begin":
                    env, i = _read_group(src, i)
                    txt, i = _convert_begin(env, src, i)
                    out.append(txt)
                elif name == "end":
                    _env, i = _read_group(src, i)
                elif name in ("text", "textrm", "mbox", "mathrm", "operatorname", "textbf", "mathit", "mathsf", "mathtt"):
                    body, i = _read_group(src, i)
                    out.append(body)
                elif name in ("left", "right", "big", "Big", "bigg", "Bigg"):
                    if i < n and src[i] in "([{}|.\\":
                        if src[i] == "\\":
                            _c, i = _read_group(src, i + 1)
                            i = i if i else i
                        else:
                            i += 1
                elif name in ("qquad", "quad"):
                    out.append("  ")
                elif name in _MATH_CMDS:
                    out.append(_MATH_CMDS[name])
                elif name == "mathbb":
                    body, i = _read_group(src, i)
                    out.append(body)
                elif name == "mathcal":
                    body, i = _read_group(src, i)
                    out.append(body)
                else:
                    out.append("")
                continue
            # single-char control like \, \; \! \{ \} \%
            if src[i + 1] in ",;!&%#~^_":
                i += 2
            elif src[i + 1] in "{}":
                i += 2
            elif src[i + 1] == " ":
                out.append(" ")
                i += 2
            else:
                i += 1
            continue
        if ch == "^":
            body, i = _read_group(src, i + 1)
            out.append(_script(_convert(body), SUPER))
            continue
        if ch == "_":
            body, i = _read_group(src, i + 1)
            out.append(_script(_convert(body), SUB))
            continue
        if ch in "{}":
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _convert_begin(env: str, src: str, i: int) -> tuple[str, int]:
    r"""Handle \begin{env} ... \end{env}; returns (text, next_index)."""
    if env != "cases":
        # unknown environment: consume until \end{env}
        end_pat = re.compile(r"\\end\{" + re.escape(env) + r"\}")
        m = end_pat.search(src, i)
        j = m.end() if m else len(src)
        return src[i:j], j
    end_pat = re.compile(r"\\end\{" + re.escape(env) + r"\}")
    m = end_pat.search(src, i)
    if not m:
        return src[i:], len(src)
    body = src[i : m.start()]
    rows: list[str] = []
    for row in re.split(r"\\\\", body):
        parts = row.split("&")
        case = parts[0].strip()
        cond = " ".join(p.strip() for p in parts[1:])
        rows.append(f"{case}  ({cond})" if cond else case)
    return "\n".join(rows), m.end()
