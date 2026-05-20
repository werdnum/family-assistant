"""Normalize assistant message text for user-facing output.

The LLM occasionally emits LaTeX-flavoured math markup such as ``$\\rightarrow$``
or ``\\alpha`` in prose. None of our delivery surfaces (Telegram, plain-text
email, the current React frontend) parse LaTeX, so users see the raw markup.

``normalize_latex_to_unicode`` rewrites known LaTeX commands to their Unicode
equivalents, strips surrounding math delimiters when their contents look
like math, and preserves markdown code spans verbatim. ``StreamingLatexNormalizer``
is the streaming counterpart used by the SSE chat endpoint.

Both paths share one tokenizer (``_consume``) that recognises constructs in a
single left-to-right pass. The streaming case is the same tokenizer with
``eof=False``; an incomplete trailing token (a partial backslash command,
an open math span, an unclosed code span) makes the tokenizer return
``None`` and the caller buffers the tail until more input arrives.

The tokeniser is intentionally conservative: text without a backslash, ``$``,
or backtick is returned unchanged, currency mentions like ``$100`` are left
alone, and unknown commands are preserved.
"""

from __future__ import annotations

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


def _is_ascii_letter(ch: str) -> bool:
    """ASCII a-zA-Z. Matches what LaTeX accepts in a control sequence name."""
    return ch.isascii() and ch.isalpha()


def _convert_commands(text: str) -> str:
    """Replace every ``\\letter+`` run with its Unicode glyph (or pass through).

    Used for content inside math delimiters; in the prose tokenizer the
    same conversion happens via ``_consume_command``.
    """
    if "\\" not in text:
        return text
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n and _is_ascii_letter(text[i + 1]):
            j = i + 1
            while j < n and _is_ascii_letter(text[j]):
                j += 1
            name = text[i + 1 : j]
            out.append(_LATEX_COMMANDS.get(name, text[i:j]))
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------
#
# ``_consume(text, i, eof)`` returns either ``(rendered, new_i)`` or ``None``.
# A ``None`` return means the construct starting at ``i`` is incomplete and
# the caller (a streaming buffer) should wait for more input.  With
# ``eof=True`` the tokenizer treats an unfinished construct as literal text
# instead, so callers can flush at end of stream.
#
# Each consumer handles a specific construct.  ``_consume`` dispatches based
# on the character at ``i``; everything else falls through to a plain text
# run via ``_consume_text``.


def _is_code_span_opener(text: str, i: int, n: int, *, eof: bool) -> bool | None:
    """Return True if backticks at ``text[i]`` open a *confirmed* code span.

    A confirmed code span is one whose closer is visible in the buffer and
    whose boundaries can be verified (no following backtick run). Used by
    math consumers to abort: a math closer that sits inside a code span
    would be extracted away before the math regex ran, so the math
    construct can't be finalised.

    Returns:
        ``True``  — a real code span begins here; the caller should abort.
        ``False`` — not a code span (just literal backticks in content).
        ``None`` — streaming mode, can't tell yet; caller should defer.
    """
    if i >= n or text[i] != "`":
        return False
    count = 0
    while i + count < n and text[i + count] == "`":
        count += 1
    end_open = i + count
    if end_open == n:
        return None if not eof else False
    closer = "`" * count
    search = end_open
    while True:
        candidate = text.find(closer, search)
        if candidate < 0:
            return None if not eof else False
        end_close = candidate + count
        if end_close < n and text[end_close] == "`":
            search = candidate + 1
            continue
        if end_close == n and not eof:
            return None
        if candidate == end_open:
            search = candidate + 1
            continue
        return True


def _consume_text(text: str, i: int, n: int) -> tuple[str, int]:
    """Consume a plain text run up to the next potentially-significant char.

    Stops on ``$``, ``\\``, or `` ` `` so the dispatcher can route to the
    right consumer.
    """
    start = i
    while i < n and text[i] not in "`$\\":
        i += 1
    return text[start:i], i


def _consume_command(text: str, i: int, n: int, *, eof: bool) -> tuple[str, int] | None:
    """Consume ``\\letter+``.

    Returns ``None`` in streaming mode when the letters extend to end of
    buffer (a following chunk might add more letters and rename the
    command).  In eof mode the command is rendered with whatever letters
    are available.
    """
    j = i + 1
    while j < n and _is_ascii_letter(text[j]):
        j += 1
    if j == n and not eof:
        return None
    name = text[i + 1 : j]
    return _LATEX_COMMANDS.get(name, text[i:j]), j


def _consume_code_span(
    text: str, i: int, n: int, *, eof: bool
) -> tuple[str, int] | None:
    """Consume `` `code` ``, `` ``code`` ``, ``` ```code``` ``` etc.

    CommonMark code spans are delimited by a "backtick string" of N
    backticks; the closer must be a backtick string of equal length not
    flanked by additional backticks. The contents are preserved verbatim.

    Returns ``None`` in streaming mode if the opener extends to end of
    buffer (might still grow), no closer is yet visible, or the closer
    sits at end of buffer (we can't yet check it isn't part of a longer
    run).
    """
    count = 0
    while i + count < n and text[i + count] == "`":
        count += 1
    end_open = i + count
    if end_open == n:
        if not eof:
            return None
        # Trailing backticks with no closer -- literal.
        return text[i:end_open], end_open

    closer = "`" * count
    search = end_open
    while True:
        candidate = text.find(closer, search)
        if candidate < 0:
            if not eof:
                return None
            return text[i:end_open], end_open
        end_close = candidate + count
        # Reject if the closer is part of a longer backtick run.
        if end_close < n and text[end_close] == "`":
            search = candidate + 1
            continue
        if end_close == n and not eof:
            # Can't verify the boundary yet.
            return None
        # CommonMark code spans require non-empty content.
        if candidate == end_open:
            search = candidate + 1
            continue
        return text[i:end_close], end_close


def _consume_display_math(
    text: str, i: int, n: int, *, eof: bool
) -> tuple[str, int] | None:
    """Consume ``$$...$$`` where the content contains a ``\\command``.

    Caller must have verified ``text[i:i+2] == "$$"`` and that the opener
    isn't preceded by ``\\``.
    """
    j = i + 2
    has_command = False
    while j < n:
        ch = text[j]
        if ch == "`":
            verdict = _is_code_span_opener(text, j, n, eof=eof)
            if verdict is None:
                return None
            if verdict:
                # The would-be ``$$`` closer would sit inside a code span
                # after extraction, so this math span doesn't exist.
                return "$$", i + 2
            # Just literal backticks; skip them as content.
            while j < n and text[j] == "`":
                j += 1
            continue
        if ch == "\\" and j + 1 < n:
            nxt = text[j + 1]
            if _is_ascii_letter(nxt):
                has_command = True
            # Skip the escaped char so e.g. ``\$`` doesn't trigger a
            # spurious closer.
            j += 2
            continue
        if ch == "$" and j + 1 < n and text[j + 1] == "$":
            if has_command:
                content = text[i + 2 : j]
                return _convert_commands(content).strip(), j + 2
            # Inner ``$$`` without a command yet -- treat as content and
            # keep looking for the real closer.
            j += 2
            continue
        j += 1
    if not eof:
        return None
    # No closer in buffer -- opener is literal.
    return "$$", i + 2


def _consume_inline_dollar(
    text: str, i: int, n: int, *, eof: bool
) -> tuple[str, int] | None:
    """Consume ``$[ \\t]*\\command...$`` on a single line.

    Caller has verified ``text[i] == "$"``, ``text[i+1] != "$"`` and that
    the opener isn't preceded by ``\\``.
    """
    probe = i + 1
    while probe < n and text[probe] in " \t":
        probe += 1
    if probe == n:
        if not eof:
            return None
        return "$", i + 1
    if text[probe] != "\\":
        # ``$5`` etc. -- currency / prose, not math.
        return "$", i + 1
    if probe + 1 == n:
        if not eof:
            return None
        return "$", i + 1
    if not _is_ascii_letter(text[probe + 1]):
        # ``$\$`` etc. -- the backslash escapes something, not a command.
        return "$", i + 1

    j = probe
    while j < n:
        ch = text[j]
        if ch == "\n":
            # ``[^$\n]*?`` in the regex forbids newlines.
            return "$", i + 1
        if ch == "`":
            verdict = _is_code_span_opener(text, j, n, eof=eof)
            if verdict is None:
                return None
            if verdict:
                # Closer would land inside the code span -- opener is literal.
                return "$", i + 1
            while j < n and text[j] == "`":
                j += 1
            continue
        if ch == "\\" and j + 1 < n:
            j += 2
            continue
        if ch == "$":
            # Closer can't be immediately followed by another ``$``.
            if j + 1 < n and text[j + 1] == "$":
                return "$", i + 1
            if j + 1 == n and not eof:
                # Can't verify the trailing ``(?!\$)`` yet.
                return None
            content = text[i + 1 : j]
            return _convert_commands(content).strip(), j + 1
        j += 1
    if not eof:
        return None
    return "$", i + 1


def _consume_paren_math(
    text: str, i: int, n: int, *, eof: bool
) -> tuple[str, int] | None:
    """Consume ``\\(...\\)`` where the content contains a ``\\command``.

    Caller has verified ``text[i:i+2] == "\\("``.
    """
    j = i + 2
    has_command = False
    while j < n:
        ch = text[j]
        if ch == "\n":
            # ``[^\n]*?`` in the regex forbids newlines.
            return text[i : i + 2], i + 2
        if ch == "`":
            verdict = _is_code_span_opener(text, j, n, eof=eof)
            if verdict is None:
                return None
            if verdict:
                return text[i : i + 2], i + 2
            while j < n and text[j] == "`":
                j += 1
            continue
        if ch == "\\" and j + 1 < n:
            nxt = text[j + 1]
            if nxt == ")":
                if has_command:
                    content = text[i + 2 : j]
                    return _convert_commands(content).strip(), j + 2
                # No command inside -- opener is literal.
                return text[i : i + 2], i + 2
            if _is_ascii_letter(nxt):
                has_command = True
            j += 2
            continue
        j += 1
    if not eof:
        return None
    return text[i : i + 2], i + 2


def _consume_bracket_math(
    text: str, i: int, n: int, *, eof: bool
) -> tuple[str, int] | None:
    """Consume ``\\[...\\]`` where the content contains a ``\\command``.

    Caller has verified ``text[i:i+2] == "\\["``. Content may span newlines
    (the regex uses DOTALL).
    """
    j = i + 2
    has_command = False
    while j < n:
        ch = text[j]
        if ch == "`":
            verdict = _is_code_span_opener(text, j, n, eof=eof)
            if verdict is None:
                return None
            if verdict:
                return text[i : i + 2], i + 2
            while j < n and text[j] == "`":
                j += 1
            continue
        if ch == "\\" and j + 1 < n:
            nxt = text[j + 1]
            if nxt == "]":
                if has_command:
                    content = text[i + 2 : j]
                    return _convert_commands(content).strip(), j + 2
                return text[i : i + 2], i + 2
            if _is_ascii_letter(nxt):
                has_command = True
            j += 2
            continue
        j += 1
    if not eof:
        return None
    return text[i : i + 2], i + 2


def _consume(text: str, i: int, n: int, *, eof: bool) -> tuple[str, int] | None:
    """Consume one token starting at ``text[i]``.

    Dispatches to the appropriate per-construct consumer. ``None`` means
    the construct is incomplete and the caller should wait for more input.
    """
    if i >= n:
        return "", i

    ch = text[i]

    if ch == "`":
        return _consume_code_span(text, i, n, eof=eof)

    if ch == "$":
        # Escaped opener -- ``\$`` was already consumed by the previous
        # ``\``-handling branch, so if we got here ``text[i-1]`` is the
        # last "real" character.
        if i > 0 and text[i - 1] == "\\":
            return "$", i + 1
        if i + 1 < n and text[i + 1] == "$":
            return _consume_display_math(text, i, n, eof=eof)
        if i + 1 == n:
            if not eof:
                return None
            return "$", i + 1
        return _consume_inline_dollar(text, i, n, eof=eof)

    if ch == "\\":
        if i + 1 == n:
            if not eof:
                return None
            return "\\", i + 1
        nxt = text[i + 1]
        if nxt == "(":
            return _consume_paren_math(text, i, n, eof=eof)
        if nxt == "[":
            return _consume_bracket_math(text, i, n, eof=eof)
        if _is_ascii_letter(nxt):
            return _consume_command(text, i, n, eof=eof)
        # ``\$``, ``\\``, ``\@`` etc. -- consume both characters as text.
        return text[i : i + 2], i + 2

    return _consume_text(text, i, n)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_latex_to_unicode(text: str) -> str:
    """Replace LaTeX math markup with Unicode equivalents.

    Returns ``text`` unchanged when no backslash is present so the common
    case is essentially free. Known commands (e.g. ``\\rightarrow``,
    ``\\alpha``) are replaced with their glyphs; unknown commands are left
    intact. Math delimiters (``$...$``, ``$$...$$``, ``\\(...\\)``,
    ``\\[...\\]``) are stripped only when their contents contain a
    backslash command, so prose like ``"$100 fee"`` is unaffected. Markdown
    code spans -- fenced ``` ```...``` ``` blocks and inline `` `...` `` --
    are preserved verbatim, so explanations of LaTeX syntax aren't
    corrupted.
    """
    if not text or "\\" not in text:
        return text
    n = len(text)
    out: list[str] = []
    i = 0
    while i < n:
        result = _consume(text, i, n, eof=True)
        if result is None:
            # ``eof=True`` should always return a value; if a consumer
            # bails defensively, emit the byte and keep going.
            out.append(text[i])
            i += 1
            continue
        rendered, next_i = result
        if next_i == i:
            # Shouldn't happen, but guard against an infinite loop.
            out.append(text[i])
            i += 1
            continue
        out.append(rendered)
        i = next_i
    return "".join(out)


class StreamingLatexNormalizer:
    """Stateful normalizer for chunked text streams.

    Token-streamed text can split LaTeX commands, math delimiters, or code
    spans across chunks. This buffer accumulates chunks, tokenizes as far
    as is safe (the same tokenizer the batch path uses), emits the
    rendered tokens, and keeps any incomplete trailing construct in the
    buffer until ``feed`` or ``flush`` resolves it.
    """

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: str) -> str:
        """Append ``chunk`` and return text that is now safe to emit."""
        if not chunk:
            return ""
        self._buffer += chunk
        rendered, consumed = _tokenize(self._buffer, eof=False)
        self._buffer = self._buffer[consumed:]
        return rendered

    def flush(self) -> str:
        """Return any remaining buffered text, normalized at end of stream."""
        if not self._buffer:
            return ""
        rendered, _ = _tokenize(self._buffer, eof=True)
        self._buffer = ""
        return rendered


def _tokenize(text: str, *, eof: bool) -> tuple[str, int]:
    """Run the tokenizer until either the input is exhausted or an
    incomplete construct is hit (only in ``eof=False`` mode).

    Returns ``(rendered, consumed)`` where ``consumed`` is the number of
    input characters successfully tokenized.
    """
    n = len(text)
    if not text:
        return "", 0
    if "\\" not in text and "$" not in text and "`" not in text:
        # Pure prose -- short-circuit the per-character loop.
        return text, n

    out: list[str] = []
    i = 0
    while i < n:
        result = _consume(text, i, n, eof=eof)
        if result is None:
            return "".join(out), i
        rendered, next_i = result
        if next_i == i:
            # Shouldn't happen; guard.
            if eof:
                out.append(text[i])
                i += 1
                continue
            return "".join(out), i
        out.append(rendered)
        i = next_i
    return "".join(out), i
