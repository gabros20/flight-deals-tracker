"""The ``--where`` tag-expression language (SEARCH-DESIGN.md §3).

Grammar (precedence: ``!`` > ``&`` > ``|``, parentheses override)::

    or_expr  := and_expr ('|' and_expr)*
    and_expr := not_expr ('&' not_expr)*
    not_expr := '!' not_expr | atom
    atom     := '(' or_expr ')' | IDENT

A bare identifier is a tag test. Aliases (e.g. ``italian`` -> ``italy``,
``greek-islands`` -> ``island & greece``) are expanded at parse time before
evaluation. Evaluation is a pure predicate over an airport's tag *set*, so it
never touches the network.

Case policy: identifier tokens are lower-cased at tokenize time, so
``Italy`` and ``italy`` are the same tag — no special-casing needed downstream,
and callers only need to report a "did you mean" hint for identifiers that
are genuinely unknown (typos), not case variants.

Adversarial input guards: expressions are capped at ``MAX_TOKENS`` tokens,
nesting (parens/``!``) is capped at ``MAX_NESTING_DEPTH``, and total
alias-expansion work is capped at ``MAX_ALIAS_EXPANSIONS`` nodes. All three
raise :class:`WhereParseError` with an actionable hint instead of letting a
pathological expression blow the Python recursion stack or fan out
exponentially through alias expansion.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Set

_TOKEN_RE = re.compile(r"\s*([&|!()]|[A-Za-z][A-Za-z0-9_-]*)")

# Adversarial-input guards (review item: RecursionError must never escape as
# a raw traceback — see tests for the exact failure modes these prevent).
MAX_TOKENS = 500
MAX_NESTING_DEPTH = 50
MAX_ALIAS_EXPANSIONS = 10_000

_OPERATORS = ("&", "|", "!", "(", ")")


class WhereParseError(ValueError):
    """Raised on a malformed where-expression. Carries an actionable ``hint``."""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint or 'try e.g. where show "seaside & (italy | spain)"'


# --------------------------------------------------------------------------- #
# AST nodes — each evaluates against a set of tags.                            #
# --------------------------------------------------------------------------- #
class Node:
    def eval(self, tags: Set[str]) -> bool:  # pragma: no cover - overridden
        raise NotImplementedError


class Tag(Node):
    def __init__(self, name: str):
        self.name = name

    def eval(self, tags: Set[str]) -> bool:
        return self.name in tags

    def __repr__(self):
        return f"Tag({self.name!r})"


class Not(Node):
    def __init__(self, child: Node):
        self.child = child

    def eval(self, tags: Set[str]) -> bool:
        return not self.child.eval(tags)

    def __repr__(self):
        return f"Not({self.child!r})"


class And(Node):
    def __init__(self, left: Node, right: Node):
        self.left, self.right = left, right

    def eval(self, tags: Set[str]) -> bool:
        return self.left.eval(tags) and self.right.eval(tags)

    def __repr__(self):
        return f"And({self.left!r}, {self.right!r})"


class Or(Node):
    def __init__(self, left: Node, right: Node):
        self.left, self.right = left, right

    def eval(self, tags: Set[str]) -> bool:
        return self.left.eval(tags) or self.right.eval(tags)

    def __repr__(self):
        return f"Or({self.left!r}, {self.right!r})"


def _tokenize(expr: str):
    tokens = []
    pos = 0
    n = len(expr)
    while pos < n:
        if expr[pos].isspace():
            pos += 1
            continue
        m = _TOKEN_RE.match(expr, pos)
        if not m:
            raise WhereParseError(
                f"unexpected character {expr[pos]!r} at position {pos}",
                hint="allowed: tags, & | ! and parentheses",
            )
        tok = m.group(1)
        # Case policy: identifiers are lower-cased here so `Italy` == `italy`
        # everywhere downstream (operators are already single symbols).
        if tok not in _OPERATORS:
            tok = tok.lower()
        tokens.append(tok)
        pos = m.end()
        if len(tokens) > MAX_TOKENS:
            raise WhereParseError(
                f"expression too long (> {MAX_TOKENS} tokens)",
                hint=f"simplify the expression to <= {MAX_TOKENS} tokens",
            )
    return tokens


def extract_identifiers(expr: str) -> List[str]:
    """Return the (lower-cased, order-preserving, de-duplicated) tag
    identifiers referenced in ``expr``, ignoring operators/parens.

    Tolerant of malformed input: on a tokenize failure, falls back to
    treating the whole (stripped, lower-cased) string as a single
    identifier so callers (e.g. unknown-tag detection) never crash on a
    bad expression they're trying to diagnose.
    """
    try:
        tokens = _tokenize(expr)
    except WhereParseError:
        stripped = (expr or "").strip().lower()
        return [stripped] if stripped else []
    seen: List[str] = []
    for t in tokens:
        if t not in _OPERATORS and t not in seen:
            seen.append(t)
    return seen


class _ParseState:
    """Shared across a top-level parse and every alias sub-parser it spawns,
    so nesting depth and total expansion work are bounded globally rather
    than per-parser-instance."""

    __slots__ = ("depth", "expanded")

    def __init__(self):
        self.depth = 0
        self.expanded = 0


class _Parser:
    def __init__(self, tokens, aliases: Dict[str, str], state: Optional[_ParseState] = None):
        self.tokens = tokens
        self.i = 0
        self.aliases = aliases or {}
        self._expanding: Set[str] = set()
        self.state = state if state is not None else _ParseState()

    def _peek(self):
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def _next(self):
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def parse(self) -> Node:
        if not self.tokens:
            raise WhereParseError("empty expression", hint="pass at least one tag, e.g. seaside")
        node = self._or()
        if self.i != len(self.tokens):
            raise WhereParseError(
                f"unexpected token {self._peek()!r}",
                hint="check operators and parentheses",
            )
        return node

    def _or(self) -> Node:
        node = self._and()
        while self._peek() == "|":
            self._next()
            node = Or(node, self._and())
        return node

    def _and(self) -> Node:
        node = self._not()
        while self._peek() == "&":
            self._next()
            node = And(node, self._not())
        return node

    def _push_depth(self):
        self.state.depth += 1
        if self.state.depth > MAX_NESTING_DEPTH:
            raise WhereParseError(
                f"expression nesting too deep (> {MAX_NESTING_DEPTH} levels of ( or !)",
                hint=f"reduce parentheses/! nesting to <= {MAX_NESTING_DEPTH}",
            )

    def _not(self) -> Node:
        if self._peek() == "!":
            self._next()
            self._push_depth()
            try:
                return Not(self._not())
            finally:
                self.state.depth -= 1
        return self._atom()

    def _atom(self) -> Node:
        tok = self._peek()
        if tok is None:
            raise WhereParseError("expression ends unexpectedly", hint="a tag or ( was expected")
        if tok == "(":
            self._next()
            self._push_depth()
            try:
                node = self._or()
            finally:
                self.state.depth -= 1
            if self._peek() != ")":
                raise WhereParseError("missing closing parenthesis", hint="balance your ( )")
            self._next()
            return node
        if tok in ("&", "|", ")"):
            raise WhereParseError(f"unexpected operator {tok!r}", hint="an operator has no left operand")
        # identifier: tag or alias
        self._next()
        return self._resolve(tok)

    def _resolve(self, name: str) -> Node:
        self.state.expanded += 1
        if self.state.expanded > MAX_ALIAS_EXPANSIONS:
            raise WhereParseError(
                f"expression/alias expansion too large (> {MAX_ALIAS_EXPANSIONS} nodes)",
                hint="simplify the expression or the alias definitions it expands through",
            )
        if name in self.aliases:
            if name in self._expanding:
                raise WhereParseError(f"alias cycle through {name!r}", hint="fix the alias definitions")
            self._expanding.add(name)
            try:
                sub = _Parser(_tokenize(self.aliases[name]), self.aliases, state=self.state)
                sub._expanding = self._expanding
                node = sub.parse()
            finally:
                self._expanding.discard(name)
            return node
        return Tag(name)


def where_parse(expr: str, aliases: Dict[str, str] | None = None) -> Node:
    """Parse ``expr`` into an evaluable AST, expanding aliases. Raises
    :class:`WhereParseError` (with ``.hint``) on malformed input."""
    if expr is None:
        raise WhereParseError("no expression given", hint="pass a tag expression")
    return _Parser(_tokenize(expr), aliases or {}).parse()
