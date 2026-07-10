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
"""
from __future__ import annotations

import re
from typing import Dict, Set

_TOKEN_RE = re.compile(r"\s*([&|!()]|[A-Za-z][A-Za-z0-9_-]*)")


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
        tokens.append(m.group(1))
        pos = m.end()
    return tokens


class _Parser:
    def __init__(self, tokens, aliases: Dict[str, str]):
        self.tokens = tokens
        self.i = 0
        self.aliases = aliases or {}
        self._expanding: Set[str] = set()

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

    def _not(self) -> Node:
        if self._peek() == "!":
            self._next()
            return Not(self._not())
        return self._atom()

    def _atom(self) -> Node:
        tok = self._peek()
        if tok is None:
            raise WhereParseError("expression ends unexpectedly", hint="a tag or ( was expected")
        if tok == "(":
            self._next()
            node = self._or()
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
        if name in self.aliases:
            if name in self._expanding:
                raise WhereParseError(f"alias cycle through {name!r}", hint="fix the alias definitions")
            self._expanding.add(name)
            try:
                sub = _Parser(_tokenize(self.aliases[name]), self.aliases)
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
