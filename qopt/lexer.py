from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .errors import LexError, point_at

KEYWORD = "KEYWORD"
IDENT = "IDENT"
NUMBER = "NUMBER"
STRING = "STRING"
OP = "OP"
COMMA = "COMMA"
DOT = "DOT"
LPAREN = "LPAREN"
RPAREN = "RPAREN"
STAR = "STAR"
EOF = "EOF"

KEYWORDS = {
    "SELECT", "DISTINCT", "FROM", "AS", "WHERE", "AND", "OR", "NOT",
    "GROUP", "BY", "HAVING", "ORDER", "ASC", "DESC",
    "JOIN", "INNER", "LEFT", "RIGHT", "OUTER", "FULL", "ON",
    "UNION", "INTERSECT", "EXCEPT", "ALL",
    "COUNT", "SUM", "AVG", "MIN", "MAX",
    "TRUE", "FALSE", "BETWEEN", "IN", "LIKE", "LIMIT", "OFFSET"
}

AGGREGATE_FUNCS = {"COUNT", "SUM", "AVG", "MIN", "MAX"}


@dataclass
class Token:
    type: str
    value: str
    pos: int

    def __str__(self) -> str:
        return f"{self.type}({self.value!r})"


def tokenize(text: str) -> List[Token]:
    tokens: List[Token] = []
    i = 0
    n = len(text)

    while i < n:
        c = text[i]

        if c.isspace():
            i += 1
            continue

        if c == ",":
            tokens.append(Token(COMMA, ",", i));
            i += 1;
            continue
        if c == ".":
            tokens.append(Token(DOT, ".", i));
            i += 1;
            continue
        if c == "(":
            tokens.append(Token(LPAREN, "(", i));
            i += 1;
            continue
        if c == ")":
            tokens.append(Token(RPAREN, ")", i));
            i += 1;
            continue
        if c == "*":
            tokens.append(Token(STAR, "*", i));
            i += 1;
            continue

        two = text[i:i + 2]
        if two in ("<=", ">=", "<>", "!="):
            tokens.append(Token(OP, two, i));
            i += 2;
            continue
        if c in ("=", "<", ">"):
            tokens.append(Token(OP, c, i));
            i += 1;
            continue

        if c == "'":
            start = i
            i += 1
            buf = []
            while i < n and text[i] != "'":
                buf.append(text[i])
                i += 1
            if i >= n:
                raise LexError("nezatvoren string literal\n" + point_at(text, start))
            i += 1
            tokens.append(Token(STRING, "".join(buf), start))
            continue

        if c.isdigit() or (c in "+-" and i + 1 < n and text[i + 1].isdigit()):
            start = i
            if c in "+-":
                i += 1
            while i < n and text[i].isdigit():
                i += 1
            if i < n and text[i] == ".":
                i += 1
                while i < n and text[i].isdigit():
                    i += 1
            tokens.append(Token(NUMBER, text[start:i], start))
            continue

        if c.isalpha() or c == "_":
            start = i
            while i < n and (text[i].isalnum() or text[i] == "_"):
                i += 1
            word = text[start:i]
            if word.upper() in KEYWORDS:
                tokens.append(Token(KEYWORD, word.upper(), start))
            else:
                tokens.append(Token(IDENT, word, start))
            continue

        raise LexError(f" Znak koji ne treba tu da se nalazi:  {c!r}\n" + point_at(text, i))

    tokens.append(Token(EOF, "", n))
    return tokens
