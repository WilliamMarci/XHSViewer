r"""LaTeX -> Unicode text converter (self-contained, no dependency).

Covers the constructs commonly found in zhihu equation images:

  - Greek letters: \alpha -> α, \Delta -> Δ ...
  - superscripts/subscripts: x^2 -> x², a_{ij} -> aᵢⱼ (unicode where
    available, ^(...)/_(...) fallback otherwise)
  - \frac{a}{b} -> (a)/(b), \sqrt{x} -> √(x)
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
    # TeX normally spells several uppercase Greek letters as Latin glyphs,
    # but accepting the explicit names makes pasted/generated LaTeX robust.
    "Alpha": "Α", "Beta": "Β", "Epsilon": "Ε", "Zeta": "Ζ",
    "Eta": "Η", "Iota": "Ι", "Kappa": "Κ", "Mu": "Μ", "Nu": "Ν",
    "Omicron": "Ο", "Rho": "Ρ", "Tau": "Τ", "Chi": "Χ",
    "omicron": "ο", "varGamma": "Γ", "varDelta": "Δ",
    "varTheta": "ϴ", "varLambda": "Λ", "varXi": "Ξ", "varPi": "Π",
    "varSigma": "Σ", "varUpsilon": "ϒ", "varPhi": "Φ",
    "varPsi": "Ψ", "varOmega": "Ω",
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
    "setminus": "∖", "smallsetminus": "∖", "wr": "≀",
    "triangleleft": "◁", "triangleright": "▷", "ltimes": "⋉",
    "rtimes": "⋊", "boxplus": "⊞", "boxminus": "⊟",
    "boxtimes": "⊠", "boxdot": "⊡",
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
    "coloneqq": "≔", "eqqcolon": "≕", "doteq": "≐", "fallingdotseq": "≒",
    "risingdotseq": "≓", "triangleq": "≜", "lesssim": "≲",
    "gtrsim": "≳", "lessapprox": "⪅", "gtrapprox": "⪆",
    "sqsubset": "⊏", "sqsupset": "⊐", "sqsubseteq": "⊑",
    "sqsupseteq": "⊒", "therefore": "∴", "because": "∵",
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
    "imath": "ı", "jmath": "ȷ", "complement": "∁", "eth": "ð",
    "Finv": "Ⅎ", "Game": "⅁", "hslash": "ℏ", "maltese": "✠",
    "diagup": "╱", "diagdown": "╲", "backepsilon": "϶",
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
        lines = r.stdout.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        lines = [re.sub(r"\[[0-9.]+(pt|cm|mm|ex|em)\]", "", ln) for ln in lines]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        if not lines:
            return None
        # Keep utftex's rows byte-for-byte (apart from outer blank lines).
        # Centering belongs to MathBlock: Textual's text-align would center
        # each row separately and break braces, arrays and fraction bars.
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
    out = _convert(src.strip())
    out = re.sub(r"\[[0-9.]+(pt|cm|mm|ex|em)\]", "", out)
    out = re.sub(r"\\\\", "", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out


# --------------------------------------------------------------------------
# recursive parser (balanced braces) used as the fallback converter
# --------------------------------------------------------------------------

_MATH_CMDS = {
    **GREEK, **OPERATORS, **RELATIONS, **ARROWS, **SYMBOLS, **LARGE_OPS,
}


def _script(txt: str, table: dict[str, str]) -> str:
    """Use Unicode scripts when complete; otherwise retain clear bounds."""
    txt = txt.strip()
    if not txt:
        return ""
    converted = "".join(table.get(char, "") for char in txt)
    if len(converted) == len(txt):
        return converted
    marker = "^" if table is SUPER else "_"
    return f"{marker}({txt})"


_STYLE_COMMANDS = {
    "mathrm", "textrm", "textnormal", "mathbf", "textbf", "mathit",
    "textit", "mathsf", "mathtt", "mathcal", "mathbb", "mathfrak",
    "mathscr", "boldsymbol", "bm", "operatorname", "mbox", "text",
}
_SIZE_COMMANDS = {
    "displaystyle", "textstyle", "scriptstyle", "scriptscriptstyle",
    "tiny", "scriptsize", "footnotesize", "small", "normalsize",
    "large", "Large", "LARGE", "huge", "Huge",
}
_DELIM_COMMANDS = {
    **DELIMITERS,
    "{": "{", "}": "}", "|": "|", ".": "", "(": "(", ")": ")",
    "[": "[", "]": "]",
}


def _split_top_level(src: str, separator: str) -> list[str]:
    """Split rows/columns without breaking nested brace groups."""
    parts: list[str] = []
    start = 0
    depth = 0
    i = 0
    while i < len(src):
        if src[i] == "{" and (i == 0 or src[i - 1] != "\\"):
            depth += 1
        elif src[i] == "}" and (i == 0 or src[i - 1] != "\\"):
            depth = max(0, depth - 1)
        elif depth == 0 and src.startswith(separator, i):
            parts.append(src[start:i])
            i += len(separator)
            start = i
            continue
        i += 1
    parts.append(src[start:])
    return parts


def _inline_environment(env: str, body: str) -> str:
    rows = _split_top_level(body, r"\\")
    cells = [
        [_convert(cell.strip()) for cell in _split_top_level(row, "&")]
        for row in rows if row.strip()
    ]
    if env == "cases":
        rendered = []
        for row in cells:
            expression = row[0] if row else ""
            condition = " ".join(row[1:]).strip()
            rendered.append(f"{expression} if {condition}" if condition else expression)
        return "{ " + "; ".join(rendered) + " }"
    if env in {"matrix", "smallmatrix", "array", "aligned", "align", "split", "gathered"}:
        left, right = "[", "]"
    elif env == "pmatrix":
        left, right = "(", ")"
    elif env == "bmatrix":
        left, right = "[", "]"
    elif env == "Bmatrix":
        left, right = "{", "}"
    elif env == "vmatrix":
        left, right = "|", "|"
    elif env == "Vmatrix":
        left, right = "‖", "‖"
    else:
        return _convert(body)
    return left + "; ".join(", ".join(row) for row in cells) + right


class _LatexParser:
    """Small recursive TeX math parser for terminal-safe inline output."""

    def __init__(self, source: str):
        self.source = source
        self.pos = 0

    def _skip_space(self) -> None:
        while self.pos < len(self.source) and self.source[self.pos].isspace():
            self.pos += 1

    def _raw_group(self) -> str:
        self._skip_space()
        if self.pos >= len(self.source):
            return ""
        if self.source[self.pos] != "{":
            start = self.pos
            if self.source[self.pos] == "\\":
                self.pos += 1
                while self.pos < len(self.source) and self.source[self.pos].isalpha():
                    self.pos += 1
            else:
                self.pos += 1
            return self.source[start:self.pos]
        self.pos += 1
        start = self.pos
        depth = 1
        while self.pos < len(self.source) and depth:
            char = self.source[self.pos]
            if char == "{" and self.source[self.pos - 1] != "\\":
                depth += 1
            elif char == "}" and self.source[self.pos - 1] != "\\":
                depth -= 1
                if depth == 0:
                    raw = self.source[start:self.pos]
                    self.pos += 1
                    return raw
            self.pos += 1
        return self.source[start:self.pos]

    def _argument(self) -> str:
        raw = self._raw_group()
        # Whitespace just inside a math argument is TeX layout, not content.
        return _convert(raw.strip())

    def _optional(self) -> str:
        self._skip_space()
        if self.pos >= len(self.source) or self.source[self.pos] != "[":
            return ""
        self.pos += 1
        start = self.pos
        depth = 1
        while self.pos < len(self.source) and depth:
            if self.source[self.pos] == "[":
                depth += 1
            elif self.source[self.pos] == "]":
                depth -= 1
                if depth == 0:
                    raw = self.source[start:self.pos]
                    self.pos += 1
                    return _convert(raw)
            self.pos += 1
        return _convert(self.source[start:self.pos])

    def _delimiter(self) -> str:
        self._skip_space()
        if self.pos >= len(self.source):
            return ""
        if self.source[self.pos] == "\\":
            self.pos += 1
            start = self.pos
            if self.pos < len(self.source) and self.source[self.pos].isalpha():
                while self.pos < len(self.source) and self.source[self.pos].isalpha():
                    self.pos += 1
            else:
                self.pos += 1
            name = self.source[start:self.pos]
            return _DELIM_COMMANDS.get(name, _MATH_CMDS.get(name, name))
        char = self.source[self.pos]
        self.pos += 1
        return "" if char == "." else char

    def _environment(self, env: str) -> str:
        end = re.compile(r"\\end\s*\{" + re.escape(env) + r"\}")
        match = end.search(self.source, self.pos)
        if match is None:
            body = self.source[self.pos:]
            self.pos = len(self.source)
        else:
            body = self.source[self.pos:match.start()]
            self.pos = match.end()
        # array has an alignment argument immediately after \begin{array}.
        if env == "array":
            nested = _LatexParser(body)
            nested._raw_group()
            body = body[nested.pos:]
        return _inline_environment(env, body)

    def _command(self) -> str:
        self.pos += 1  # backslash
        if self.pos >= len(self.source):
            return ""
        if not self.source[self.pos].isalpha():
            char = self.source[self.pos]
            self.pos += 1
            return {
                "{": "{", "}": "}", "%": "%", "#": "#", "&": "&",
                "_": "_", "$": "$", " ": " ", ",": " ", ":": " ",
                ";": " ", "!": "", "\\": " ",
            }.get(char, char)
        start = self.pos
        while self.pos < len(self.source) and self.source[self.pos].isalpha():
            self.pos += 1
        name = self.source[start:self.pos]
        if self.pos < len(self.source) and self.source[self.pos] == "*":
            self.pos += 1
        # Spaces after a control word delimit its name; they are not output.
        self._skip_space()
        if name in {"frac", "dfrac", "tfrac", "cfrac"}:
            numerator = self._argument()
            denominator = self._argument()
            return f"({numerator})/({denominator})"
        if name in {"binom", "dbinom", "tbinom"}:
            return f"C({_convert(self._raw_group())}, {_convert(self._raw_group())})"
        if name == "sqrt":
            degree = self._optional()
            body = self._argument()
            return f"{degree}√({body})" if degree else f"√({body})"
        if name in DECOR:
            return self._argument() + DECOR[name]
        if name in _STYLE_COMMANDS:
            raw = self._raw_group()
            return raw if name in {"text", "mbox", "textrm", "textnormal"} else _convert(raw)
        if name == "begin":
            return self._environment(self._raw_group().strip())
        if name == "end":
            self._raw_group()
            return ""
        if name in {"left", "right", "big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr", "biggl", "biggr"}:
            return self._delimiter()
        if name in {"quad", "qquad", "enspace", "enskip"}:
            return " "
        if name in {"hspace", "hspace*", "vspace", "kern", "mkern", "rule", "label", "tag"}:
            self._raw_group()
            return ""
        if name in _SIZE_COMMANDS:
            return ""
        if name in {"pmod", "pod"}:
            return f" (mod {self._argument()})"
        if name in _MATH_CMDS:
            return _MATH_CMDS[name]
        # Preserve unsupported commands visibly instead of silently deleting
        # mathematical identifiers generated by new LaTeX packages.
        return name

    def parse(self) -> str:
        output: list[str] = []
        while self.pos < len(self.source):
            char = self.source[self.pos]
            if char == "\\":
                output.append(self._command())
            elif char == "{":
                output.append(_convert(self._raw_group()))
            elif char == "}":
                self.pos += 1
            elif char in "^_":
                self.pos += 1
                output.append(_script(self._argument(), SUPER if char == "^" else SUB))
            elif char.isspace():
                self._skip_space()
                output.append(" ")
            elif char == "~":
                self.pos += 1
                output.append(" ")
            else:
                output.append(char)
                self.pos += 1
        return "".join(output)


def _convert(src: str) -> str:
    return _LatexParser(src).parse()
