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

_INLINE_DOLLAR_MATH_RE = re.compile(
    r"(?<!\\)\$(?!\$)([^$\n]*?\\[a-zA-Z]+[^$\n]*?)(?<!\\)\$(?!\$)"
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


def normalize_latex_to_unicode(text: str) -> str:
    """Replace LaTeX math markup with Unicode equivalents.

    Returns ``text`` unchanged when no backslash is present so the common case
    is essentially free. Known commands (e.g. ``\\rightarrow``, ``\\alpha``) are
    replaced with their glyphs; unknown commands are left intact. Math
    delimiters (``$...$``, ``$$...$$``, ``\\(...\\)``, ``\\[...\\]``) are
    stripped only when their contents contain a backslash command, so prose
    like ``"$100 fee"`` is unaffected.
    """
    if not text or "\\" not in text:
        return text

    text = _DISPLAY_DOLLAR_MATH_RE.sub(_strip_delims, text)
    text = _INLINE_DOLLAR_MATH_RE.sub(_strip_delims, text)
    text = _BRACKET_MATH_RE.sub(_strip_delims, text)
    text = _PAREN_MATH_RE.sub(_strip_delims, text)
    return _convert_commands(text)
