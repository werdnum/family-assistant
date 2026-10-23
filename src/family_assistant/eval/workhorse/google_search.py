"""Gmail and Drive query semantics for the eval's fake Google.

Each query is parsed into a small boolean tree and evaluated against one
world item with three-valued logic: a term the fake cannot decide (an
unmodelled operator, attachment metadata for a message only ever listed) is
``None`` and never rules an item out on its own.

Text the world lacks is the main limit: most messages were only ever listed,
so their body is a snippet. Where text is missing, the fake falls back on
evidence -- the recorded searches whose real results included the item are
Google's own verdict that it matches their terms. A replay of a recorded
search withholds that search's own evidence, so conformance against the
recording is measured, not assumed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable

    from family_assistant.eval.workhorse.google_world import WorldFile, WorldMessage

__all__ = ["drive_known_folders", "drive_query_matches", "gmail_query_matches"]

Truth = bool | None


@dataclass(frozen=True)
class _Term:
    operator: str | None
    value: str
    phrase: bool


@dataclass(frozen=True)
class _Not:
    child: object


@dataclass(frozen=True)
class _All:
    children: tuple[object, ...]


@dataclass(frozen=True)
class _Any:
    children: tuple[object, ...]


def _and(values: list[Truth]) -> Truth:
    if any(v is False for v in values):
        return False
    return True if values else None


def _or(values: list[Truth]) -> Truth:
    if any(v is True for v in values):
        return True
    if any(v is None for v in values):
        return None
    return False


def _not(value: Truth) -> Truth:
    return None if value is None else not value


def _words_match(needle: str, haystack: str) -> bool:
    """Whether the words of ``needle`` appear as whole words of ``haystack``, in order."""
    words = re.findall(r"\w+", needle.lower())
    if not words:
        return True
    pattern = r"\W+".join(rf"\b{re.escape(word)}\b" for word in words)
    return re.search(pattern, haystack.lower()) is not None


# --------------------------------------------------------------------- Gmail

_GMAIL_TOKEN = re.compile(
    r"\s*(?:(?P<lparen>\()|(?P<rparen>\))|(?P<lbrace>\{)|(?P<rbrace>\})"
    r'|(?P<neg>-)(?=\S)|(?P<op>[A-Za-z_]+):(?=\S)|"(?P<phrase>[^"]*)"|(?P<word>[^\s(){}"]+))'
)


def _gmail_tokens(query: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(query):
        match = _GMAIL_TOKEN.match(query, position)
        if match is None or match.end() == position:
            break
        position = match.end()
        kind = match.lastgroup
        assert kind is not None
        tokens.append((kind, match.group(kind)))
    return tokens


class _GmailParser:
    def __init__(self, query: str) -> None:
        self.tokens = _gmail_tokens(query)
        self.index = 0

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def take(self) -> tuple[str, str]:
        token = self.tokens[self.index]
        self.index += 1
        return token

    def next_is(self, kind: str) -> bool:
        token = self.peek()
        return token is not None and token[0] == kind

    def parse(self) -> object:
        node = self.disjunction(closing=None)
        return node

    def disjunction(self, closing: str | None) -> object:
        # Gmail's OR binds tighter than the implied AND: ``a b OR c`` is
        # ``a (b OR c)``, so a query is an AND of OR-chains.
        return self.conjunction(closing)

    def conjunction(self, closing: str | None) -> object:
        children: list[object] = []
        while (token := self.peek()) is not None:
            kind, value = token
            if kind == closing:
                break
            if kind in {"rparen", "rbrace"} or (kind == "word" and value == "OR"):
                self.take()
                continue
            child = self.or_chain(closing)
            if child is not None:
                children.append(child)
        return children[0] if len(children) == 1 else _All(tuple(children))

    def or_chain(self, closing: str | None) -> object | None:
        alternatives = [self.unary()]
        while (token := self.peek()) and token == ("word", "OR"):
            self.take()
            following = self.peek()
            if following is None or following[0] == closing:
                break
            alternatives.append(self.unary())
        present = [a for a in alternatives if a is not None]
        if not present:
            return None
        return present[0] if len(present) == 1 else _Any(tuple(present))

    def unary(self) -> object | None:
        kind, value = self.take()
        if kind == "neg":
            if self.peek() is None:
                return None
            child = self.unary()
            return None if child is None else _Not(child)
        if kind == "lparen":
            node = self.disjunction(closing="rparen")
            if self.next_is("rparen"):
                self.take()
            return node
        if kind == "lbrace":
            node = self.conjunction(closing="rbrace")
            if self.next_is("rbrace"):
                self.take()
            children = node.children if isinstance(node, _All) else (node,)
            return _Any(tuple(children))
        if kind == "op":
            return self.operator(value.lower())
        if kind == "phrase":
            return _Term(None, value, phrase=True)
        if value == "AND":
            return None
        return _Term(None, value, phrase=False)

    def operator(self, operator: str) -> object | None:
        token = self.peek()
        if token is None:
            return None
        kind, value = self.take()
        if kind == "lparen":
            inner = self.disjunction(closing="rparen")
            if self.next_is("rparen"):
                self.take()
            return _bind_operator(inner, operator)
        if kind == "phrase":
            return _Term(operator, value, phrase=True)
        return _Term(operator, value, phrase=False)


def _bind_operator(node: object, operator: str) -> object:
    """``from:(a OR b)`` applies the operator to every term inside."""
    if isinstance(node, _Term):
        return _Term(node.operator or operator, node.value, node.phrase)
    if isinstance(node, _Not):
        return _Not(_bind_operator(node.child, operator))
    if isinstance(node, _All):
        return _All(tuple(_bind_operator(c, operator) for c in node.children))
    if isinstance(node, _Any):
        return _Any(tuple(_bind_operator(c, operator) for c in node.children))
    return node


@lru_cache(maxsize=4096)
def _parse_gmail(query: str) -> object:
    return _GmailParser(query).parse()


@lru_cache(maxsize=4096)
def _conjunctive_terms(query: str) -> frozenset[str]:
    """Free words every result of ``query`` must contain: its plain ANDed terms."""
    node = _parse_gmail(query)
    children = node.children if isinstance(node, _All) else (node,)
    words: set[str] = set()
    for child in children:
        if isinstance(child, _Term) and child.operator is None:
            words.update(re.findall(r"\w+", child.value.lower()))
    return frozenset(words)


def _gmail_date(value: str, tz: tzinfo | None) -> datetime | None:
    pieces = re.split(r"[/-]", value)
    if len(pieces) != 3 or not all(piece.isdigit() for piece in pieces):
        return None
    year, month, day = (int(piece) for piece in pieces)
    try:
        return datetime(year, month, day, tzinfo=tz or UTC)
    except ValueError:
        return None


_RELATIVE_AGE = re.compile(r"^(\d+)([dmy])$")


def _message_text(message: WorldMessage) -> str:
    return " ".join(
        part or ""
        for part in (
            message.subject,
            message.from_,
            message.to,
            message.snippet,
            message.body,
            " ".join(a.filename for a in message.attachments),
        )
    )


@dataclass
class _GmailContext:
    now: datetime
    evidence: frozenset[str]
    # The user's own addresses, for ``me``.
    me: tuple[str, ...]
    unmodelled: set[str]


def _addressed(value: str, header: str | None, me: tuple[str, ...]) -> bool:
    if value.lower() == "me":
        return any(address.lower() in (header or "").lower() for address in me)
    return _words_match(value, header or "")


def _gmail_term(term: _Term, message: WorldMessage, context: _GmailContext) -> Truth:
    now, evidence = context.now, context.evidence
    operator, value = term.operator, term.value
    if operator is None:
        if _words_match(value, _message_text(message)):
            return True
        words = re.findall(r"\w+", value.lower())
        return bool(words) and all(word in evidence for word in words)
    sent = message.sent_at or message.first_seen_at
    if operator == "from":
        return _addressed(value, message.from_, context.me)
    if operator in {"to", "cc", "bcc", "deliveredto"}:
        return _addressed(value, message.to, context.me)
    lowered = value.lower()
    if operator == "in" and lowered in {"inbox", "all", "anywhere"}:
        return True
    if (operator, lowered) in {("in", "sent"), ("is", "sent")}:
        return _addressed("me", message.from_, context.me)
    if (operator, lowered) in {
        ("in", "drafts"),
        ("is", "draft"),
        ("in", "trash"),
        ("in", "spam"),
    }:
        # The world holds delivered mail only.
        return False
    if operator == "subject":
        return _words_match(value, message.subject or "")
    if operator in {"after", "before", "newer", "older"}:
        bound = (
            datetime.fromtimestamp(int(value), tz=UTC)
            if value.isdigit()
            else _gmail_date(value, sent.tzinfo)
        )
        if bound is None:
            return None
        return sent >= bound if operator in {"after", "newer"} else sent < bound
    if operator in {"newer_than", "older_than"}:
        match = _RELATIVE_AGE.match(value.lower())
        if match is None:
            return None
        days = int(match[1]) * {"d": 1, "m": 30, "y": 365}[match[2]]
        cutoff = now - timedelta(days=days)
        return sent >= cutoff if operator == "newer_than" else sent < cutoff
    if operator in {"has", "filename"} and message.body is None:
        # A listing carries no attachment metadata.
        return None
    if operator == "has" and value.lower() == "attachment":
        return bool(message.attachments)
    if operator == "filename":
        return any(_words_match(value, a.filename) for a in message.attachments)
    # label:, is:unread, category:, larger:, list: and the rest are not
    # modelled. They neither include nor exclude, and the trial is told.
    context.unmodelled.add(f"{operator}:{value}")
    return None


def _evaluate[L](node: object, leaf: type[L], decide: Callable[[L], Truth]) -> Truth:
    if isinstance(node, leaf):
        return decide(node)
    if isinstance(node, _Not):
        return _not(_evaluate(node.child, leaf, decide))
    if isinstance(node, _All):
        return _and([_evaluate(c, leaf, decide) for c in node.children])
    if isinstance(node, _Any):
        return _or([_evaluate(c, leaf, decide) for c in node.children])
    return None


def _evidence(matched: list[str], withheld: Collection[str]) -> frozenset[str]:
    words: set[str] = set()
    for query in matched:
        if query not in withheld:
            words |= _conjunctive_terms(query)
    return frozenset(words)


def gmail_query_matches(
    message: WorldMessage,
    query: str,
    now: datetime,
    withheld: Collection[str] = (),
    *,
    me: Collection[str] = (),
    unmodelled: set[str] | None = None,
) -> bool:
    """Whether Gmail would return ``message`` for ``query``, as best the world shows.

    Operators the fake cannot decide are added to ``unmodelled``.
    """
    if query in message.matched_queries and query not in withheld:
        return True
    context = _GmailContext(
        now=now,
        evidence=_evidence(message.matched_queries, withheld),
        me=tuple(me),
        unmodelled=unmodelled if unmodelled is not None else set(),
    )
    result = _evaluate(
        _parse_gmail(query), _Term, lambda term: _gmail_term(term, message, context)
    )
    return result is not False


# --------------------------------------------------------------------- Drive

_DRIVE_TOKEN = re.compile(
    r"\s*(?:(?P<lparen>\()|(?P<rparen>\))|'(?P<string>(?:[^'\\]|\\.)*)'"
    r"|(?P<cmp>!=|>=|<=|=|<|>)|(?P<word>[A-Za-z_.]+))"
)


def _drive_tokens(query: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(query):
        match = _DRIVE_TOKEN.match(query, position)
        if match is None or match.end() == position:
            position += 1
            continue
        position = match.end()
        kind = match.lastgroup
        assert kind is not None
        value = match.group(kind)
        tokens.append((kind, value.replace("\\'", "'") if kind == "string" else value))
    return tokens


@dataclass(frozen=True)
class _Clause:
    field: str
    comparison: str
    value: str


class _DriveParser:
    def __init__(self, query: str) -> None:
        self.tokens = _drive_tokens(query)
        self.index = 0

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def take(self) -> tuple[str, str]:
        token = self.tokens[self.index]
        self.index += 1
        return token

    def next_is(self, kind: str) -> bool:
        token = self.peek()
        return token is not None and token[0] == kind

    def disjunction(self) -> object:
        groups = [self.conjunction()]
        while (
            (token := self.peek()) and token[0] == "word" and token[1].lower() == "or"
        ):
            self.take()
            groups.append(self.conjunction())
        return groups[0] if len(groups) == 1 else _Any(tuple(groups))

    def conjunction(self) -> object:
        children = [self.unary()]
        while (
            (token := self.peek()) and token[0] == "word" and token[1].lower() == "and"
        ):
            self.take()
            children.append(self.unary())
        children = [c for c in children if c is not None]
        return children[0] if len(children) == 1 else _All(tuple(children))

    def unary(self) -> object | None:
        token = self.peek()
        if token is None:
            return None
        kind, value = token
        if kind == "word" and value.lower() == "not":
            self.take()
            child = self.unary()
            return None if child is None else _Not(child)
        if kind == "lparen":
            self.take()
            node = self.disjunction()
            if self.next_is("rparen"):
                self.take()
            return node
        return self.clause()

    def clause(self) -> object | None:
        parts: list[tuple[str, str]] = []
        while (token := self.peek()) is not None:
            if token[0] in {"rparen"} or (
                token[0] == "word" and token[1].lower() in {"and", "or"}
            ):
                break
            parts.append(self.take())
            if token[0] == "lparen":
                break
        if len(parts) >= 3 and parts[0][0] == "string" and parts[1] == ("word", "in"):
            return _Clause(parts[2][1], "in", parts[0][1])
        if len(parts) >= 3 and parts[0][0] == "word":
            field, (kind, comparison), value = parts[0][1], parts[1], parts[2][1]
            if kind in {"cmp", "word"}:
                return _Clause(field, comparison.lower(), value)
        if len(parts) == 1 and parts[0][0] == "word":
            return _Clause(parts[0][1], "flag", "")
        return None


@lru_cache(maxsize=4096)
def _parse_drive(query: str) -> object:
    return _DriveParser(query).disjunction()


@lru_cache(maxsize=4096)
def _drive_conjunctive_clauses(query: str) -> frozenset[_Clause]:
    node = _parse_drive(query)
    children = node.children if isinstance(node, _All) else (node,)
    if not all(isinstance(child, _Clause) for child in children):
        return frozenset()
    return frozenset(c for c in children if isinstance(c, _Clause))


@dataclass
class _DriveContext:
    evidence: frozenset[_Clause]
    # Folders whose recorded listing is known, so membership is closed.
    known_folders: frozenset[str]
    unmodelled: set[str]


def _drive_clause(clause: _Clause, file: WorldFile, context: _DriveContext) -> Truth:
    evidence = context.evidence
    field, comparison, value = clause.field, clause.comparison, clause.value
    if field in {"name", "fullText"} and comparison == "contains":
        text = file.name if field == "name" else f"{file.name} {file.content or ''}"
        # Strict where the text is missing, as for Gmail bodies: only a
        # recorded verdict can place a file the world holds no text for.
        return value.lower() in text.lower() or clause in evidence
    if field == "name" and comparison in {"=", "!="}:
        same = file.name == value
        return same if comparison == "=" else not same
    if field == "mimeType" and comparison in {"=", "!="}:
        same = file.mime_type == value
        return same if comparison == "=" else not same
    if field == "parents" and comparison == "in":
        # The world records no parents; only a recorded listing of the
        # folder places a file in it. An unlisted folder is empty here, and
        # the trial is told, rather than matching the whole Drive.
        if clause in evidence:
            return True
        if value not in context.known_folders:
            context.unmodelled.add(f"unlisted folder {value}")
        return False
    if field == "owners" and comparison == "in" and value == "me":
        return True
    if field == "modifiedTime" and comparison in {">", ">=", "<", "<="}:
        try:
            bound = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if file.modified_time is None:
            return None
        if bound.tzinfo is None:
            bound = bound.replace(tzinfo=UTC)
        moment = file.modified_time
        return {
            ">": moment > bound,
            ">=": moment >= bound,
            "<": moment < bound,
            "<=": moment <= bound,
        }[comparison]
    if field == "trashed":
        return value.lower() == "false"
    context.unmodelled.add(f"{field} {comparison} {value}".strip())
    return None


def _file_evidence(file: WorldFile, withheld: Collection[str]) -> frozenset[_Clause]:
    evidence: set[_Clause] = set()
    for matched in file.matched_queries:
        if matched not in withheld:
            evidence |= _drive_conjunctive_clauses(matched)
    return frozenset(evidence)


def drive_known_folders(
    files: Iterable[WorldFile], withheld: Collection[str] = ()
) -> frozenset[str]:
    """Folders some recorded listing enumerated, so their membership is known."""
    return frozenset(
        clause.value
        for file in files
        for clause in _file_evidence(file, withheld)
        if clause.field == "parents" and clause.comparison == "in"
    )


def drive_query_matches(
    file: WorldFile,
    query: str,
    withheld: Collection[str] = (),
    *,
    known_folders: Collection[str] = (),
    unmodelled: set[str] | None = None,
) -> bool:
    """Whether Drive would return ``file`` for ``query``, as best the world shows."""
    if query in file.matched_queries and query not in withheld:
        return True
    context = _DriveContext(
        evidence=_file_evidence(file, withheld),
        known_folders=frozenset(known_folders),
        unmodelled=unmodelled if unmodelled is not None else set(),
    )
    result = _evaluate(
        _parse_drive(query),
        _Clause,
        lambda clause: _drive_clause(clause, file, context),
    )
    return result is not False
