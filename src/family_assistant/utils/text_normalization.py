"""Normalize assistant message text for user-facing output.

The LLM occasionally emits LaTeX-flavoured math markup such as ``$\\rightarrow$``
or ``\\alpha`` in prose. None of our delivery surfaces (Telegram, plain-text
email, the current React frontend) parse LaTeX, so users see the raw markup.

``normalize_latex_to_unicode`` rewrites known LaTeX commands to their Unicode
equivalents and strips surrounding math delimiters when their contents look
like math. It is intentionally conservative: text without a backslash is
returned unchanged, currency mentions like ``$100`` are left alone, and
unknown commands are preserved.
"""

from __future__ import annotations

import re

_LATEX_COMMANDS: dict[str, str] = {
    # Arrows
    "rightarrow": "→",
    "to": "→",
    "longrightarrow": "⟶",
    "Rightarrow": "⇒",
    "implies": "⇒",
    "Longrightarrow": "⟹",
    "leftarrow": "←",
    "gets": "←",
    "longleftarrow": "⟵",
    "Leftarrow": "⇐",
    "Longleftarrow": "⟸",
    "leftrightarrow": "↔",
    "longleftrightarrow": "⟷",
    "Leftrightarrow": "⇔",
    "iff": "⇔",
    "Longleftrightarrow": "⟺",
    "uparrow": "↑",
    "downarrow": "↓",
    "Uparrow": "⇑",
    "Downarrow": "⇓",
    "updownarrow": "↕",
    "mapsto": "↦",
    "hookrightarrow": "↪",
    "hookleftarrow": "↩",
    # Lowercase Greek
    "alpha": "α",
    "beta": "β",
    "gamma": "γ",
    "delta": "δ",
    "epsilon": "ε",
    "varepsilon": "ε",
    "zeta": "ζ",
    "eta": "η",
    "theta": "θ",
    "vartheta": "ϑ",
    "iota": "ι",
    "kappa": "κ",
    "lambda": "λ",
    "mu": "μ",
    "nu": "ν",
    "xi": "ξ",
    "omicron": "ο",
    "pi": "π",
    "varpi": "ϖ",
    "rho": "ρ",
    "varrho": "ϱ",
    "sigma": "σ",
    "varsigma": "ς",
    "tau": "τ",
    "upsilon": "υ",
    "phi": "φ",
    "varphi": "φ",
    "chi": "χ",
    "psi": "ψ",
    "omega": "ω",
    # Uppercase Greek
    "Gamma": "Γ",
    "Delta": "Δ",
    "Theta": "Θ",
    "Lambda": "Λ",
    "Xi": "Ξ",
    "Pi": "Π",
    "Sigma": "Σ",
    "Upsilon": "Υ",
    "Phi": "Φ",
    "Psi": "Ψ",
    "Omega": "Ω",
    # Binary operators
    "times": "×",
    "div": "÷",
    "pm": "±",
    "mp": "∓",
    "cdot": "·",
    "ast": "∗",
    "star": "⋆",
    "circ": "∘",
    "bullet": "•",
    "oplus": "⊕",
    "ominus": "⊖",
    "otimes": "⊗",
    "odot": "⊙",
    # Relations
    "le": "≤",
    "leq": "≤",
    "ge": "≥",
    "geq": "≥",
    "ne": "≠",
    "neq": "≠",
    "equiv": "≡",
    "approx": "≈",
    "sim": "∼",
    "simeq": "≃",
    "cong": "≅",
    "propto": "∝",
    "subset": "⊂",
    "supset": "⊃",
    "subseteq": "⊆",
    "supseteq": "⊇",
    "in": "∈",
    "notin": "∉",
    "ni": "∋",
    "perp": "⊥",
    "parallel": "∥",
    # Set / logic
    "cup": "∪",
    "cap": "∩",
    "emptyset": "∅",
    "varnothing": "∅",
    "forall": "∀",
    "exists": "∃",
    "nexists": "∄",
    "neg": "¬",
    "lnot": "¬",
    "land": "∧",
    "wedge": "∧",
    "lor": "∨",
    "vee": "∨",
    # Big operators
    "sum": "∑",
    "prod": "∏",
    "coprod": "∐",
    "int": "∫",
    "iint": "∬",
    "iiint": "∭",
    "oint": "∮",
    # Miscellaneous
    "infty": "∞",
    "partial": "∂",
    "nabla": "∇",
    "hbar": "ℏ",
    "ell": "ℓ",
    "Re": "ℜ",
    "Im": "ℑ",
    "aleph": "ℵ",
    "dagger": "†",
    "ddagger": "‡",
    "ldots": "…",
    "dots": "…",
    "cdots": "⋯",
    "vdots": "⋮",
    "ddots": "⋱",
    "degree": "°",
    "checkmark": "✓",
}

_LATEX_COMMAND_RE = re.compile(r"\\([a-zA-Z]+)")

# Inline ``$...$`` is only treated as math when the content starts with a
# backslash command. This avoids corrupting currency mentions like
# ``"Price $5 and \alpha cost $10."`` where the dollars are not math
# delimiters.
_INLINE_DOLLAR_MATH_RE = re.compile(
    r"(?<!\\)\$(?!\$)(\\[a-zA-Z]+[^$\n]*?)(?<!\\)\$(?!\$)"
)

_DISPLAY_DOLLAR_MATH_RE = re.compile(
    r"\$\$(.*?\\[a-zA-Z]+.*?)\$\$",
    re.DOTALL,
)

_PAREN_MATH_RE = re.compile(r"\\\(([^\n]*?\\[a-zA-Z]+[^\n]*?)\\\)")

_BRACKET_MATH_RE = re.compile(
    r"\\\[(.*?\\[a-zA-Z]+.*?)\\\]",
    re.DOTALL,
)


def _replace_command(match: re.Match[str]) -> str:
    name = match.group(1)
    replacement = _LATEX_COMMANDS.get(name)
    if replacement is None:
        return match.group(0)
    return replacement


def _convert_commands(text: str) -> str:
    return _LATEX_COMMAND_RE.sub(_replace_command, text)


def _strip_delims(match: re.Match[str]) -> str:
    return _convert_commands(match.group(1))


# Markdown code spans whose contents must be preserved verbatim. Fenced blocks
# come first so a stray backtick inside a fence doesn't terminate it early.
_CODE_SPAN_RE = re.compile(
    r"```[\s\S]*?```"  # fenced code block
    r"|`[^`\n]+`"  # inline code span
)


def _normalize_segment(text: str) -> str:
    """Apply math/command substitutions to a non-code segment."""
    if not text or "\\" not in text:
        return text
    text = _DISPLAY_DOLLAR_MATH_RE.sub(_strip_delims, text)
    text = _INLINE_DOLLAR_MATH_RE.sub(_strip_delims, text)
    text = _BRACKET_MATH_RE.sub(_strip_delims, text)
    text = _PAREN_MATH_RE.sub(_strip_delims, text)
    return _convert_commands(text)


def normalize_latex_to_unicode(text: str) -> str:
    """Replace LaTeX math markup with Unicode equivalents.

    Returns ``text`` unchanged when no backslash is present so the common case
    is essentially free. Known commands (e.g. ``\\rightarrow``, ``\\alpha``) are
    replaced with their glyphs; unknown commands are left intact. Math
    delimiters (``$...$``, ``$$...$$``, ``\\(...\\)``, ``\\[...\\]``) are
    stripped only when their contents contain a backslash command, so prose
    like ``"$100 fee"`` is unaffected. Markdown code spans -- fenced
    ``` ```...``` ``` blocks and inline `` `...` `` -- are preserved verbatim,
    so explanations of LaTeX syntax aren't corrupted.
    """
    if not text or "\\" not in text:
        return text

    pieces: list[str] = []
    cursor = 0
    for match in _CODE_SPAN_RE.finditer(text):
        pieces.append(_normalize_segment(text[cursor : match.start()]))
        pieces.append(match.group(0))
        cursor = match.end()
    pieces.append(_normalize_segment(text[cursor:]))
    return "".join(pieces)


# Patterns describing where the trailing edge of a buffered stream might still
# be inside an incomplete construct that must not be normalized in isolation.
_TRAILING_BARE_COMMAND_RE = re.compile(r"\\[a-zA-Z]*\Z")
_TRAILING_OPEN_PAREN_MATH_RE = re.compile(r"\\\([^\n]*\Z")
_TRAILING_OPEN_BRACKET_MATH_RE = re.compile(r"\\\[[\s\S]*\Z")


def _last_unmatched(text: str, marker: str, *, line_scoped: bool) -> int:
    """Return the position of the last unmatched ``marker`` occurrence.

    ``line_scoped`` resets the open/close state at every newline (matches
    Markdown inline conventions for ``$`` and inline backticks).
    """
    open_pos = -1
    in_span = False
    for i, ch in enumerate(text):
        if line_scoped and ch == "\n":
            open_pos = -1
            in_span = False
            continue
        if ch == "\\" and i + 1 < len(text):
            # Skip escaped character.
            continue
        if ch == marker:
            if in_span:
                in_span = False
            else:
                in_span = True
                open_pos = i
    return open_pos if in_span else -1


def _streaming_safe_split(buffer: str) -> int:
    """Find the position up to which ``buffer`` can be normalized in isolation.

    Anything after the returned position may still be inside an open math
    span, code span, or partial backslash command, so it must wait for more
    input. Returns ``len(buffer)`` when the entire buffer is safe to emit.
    """
    if not buffer:
        return 0

    candidates: list[int] = [len(buffer)]

    bare = _TRAILING_BARE_COMMAND_RE.search(buffer)
    if bare:
        candidates.append(bare.start())

    paren = _TRAILING_OPEN_PAREN_MATH_RE.search(buffer)
    if paren and r"\)" not in paren.group(0):
        candidates.append(paren.start())

    bracket = _TRAILING_OPEN_BRACKET_MATH_RE.search(buffer)
    if bracket and r"\]" not in bracket.group(0):
        candidates.append(bracket.start())

    dollar = _last_unmatched(buffer, "$", line_scoped=True)
    if dollar >= 0:
        candidates.append(dollar)

    backtick = _last_unmatched(buffer, "`", line_scoped=True)
    if backtick >= 0:
        candidates.append(backtick)

    fence_positions = [m.start() for m in re.finditer(r"```", buffer)]
    if len(fence_positions) % 2 == 1:
        candidates.append(fence_positions[-1])

    return min(candidates)


class StreamingLatexNormalizer:
    """Stateful normalizer for chunked text streams.

    Token-streamed text can split LaTeX commands, math delimiters, or code
    spans across chunks. Per-chunk normalization would leak partially-rewritten
    markup. This buffer holds back the tail of each chunk while it might still
    extend an incomplete construct, normalizes the safe prefix, and flushes
    the remainder at end of stream.
    """

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: str) -> str:
        """Append ``chunk`` and return text that is now safe to emit."""
        if not chunk:
            return ""
        self._buffer += chunk
        split = _streaming_safe_split(self._buffer)
        if split <= 0:
            return ""
        emit = self._buffer[:split]
        self._buffer = self._buffer[split:]
        return normalize_latex_to_unicode(emit)

    def flush(self) -> str:
        """Return any remaining buffered text normalized at end of stream."""
        remaining = self._buffer
        self._buffer = ""
        return normalize_latex_to_unicode(remaining)
