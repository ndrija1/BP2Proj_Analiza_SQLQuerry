from __future__ import annotations

from typing import List, Optional

from . import ast_nodes as A
from .ast_nodes import And, Comparison,Or
from .errors import ParseError, point_at
from .lexer import (
    AGGREGATE_FUNCS, COMMA, DOT, EOF, IDENT, KEYWORD, LPAREN, NUMBER,
    OP, RPAREN, STAR, STRING, Token, tokenize,
)


def parse(text: str) -> A.Query:
    return Parser(text).parse()


class Parser:
    def __init__(self, text: str):
        self.text = text
        self.tokens = tokenize(text)
        self.pos = 0


    def peek(self, offset: int = 0) -> Token:
        return self.tokens[min(self.pos + offset, len(self.tokens) - 1)]

    def advance(self) -> Token:
        tok = self.tokens[self.pos]
        if tok.type != EOF:
            self.pos += 1
        return tok

    def at(self, type_: str, value: Optional[str] = None) -> bool:
        t = self.peek()
        return t.type == type_ and (value is None or t.value == value)

    def at_keyword(self, *keywords: str) -> bool:
        t = self.peek()
        return t.type == KEYWORD and t.value in keywords

    def match(self, type_: str, value: Optional[str] = None) -> bool:
        """Potrosi token ako se poklapa, inace ga ostavi. Za opcione delove."""
        if self.at(type_, value):
            self.advance()
            return True
        return False

    def expect(self, type_: str, value: Optional[str] = None) -> Token:
        """Token MORA da se poklopi, inace sintaksna greska. Za obavezne delove."""
        if not self.at(type_, value):
            want = value if value is not None else type_
            self.error(f"očekivano {want!r}")
        return self.advance()

    def error(self, msg: str):
        t = self.peek()
        got = "kraj ulaza" if t.type == EOF else f"{t.value!r}"
        raise ParseError(
            f"sintaksna greška: {msg}, a pronađeno {got}\n"
            + point_at(self.text, t.pos)
        )


    def parse(self) -> A.Query:
        query = self.parse_set_query()
        if not self.at(EOF):
            self.error("neocekivan tekst posle upita")
        return query

    def parse_set_query(self) -> A.Query:
        left: A.Query = self.parse_select_core()
        while self.at_keyword("UNION", "INTERSECT", "EXCEPT"):
            op = self.advance().value
            is_all = self.match(KEYWORD, "ALL")
            right = self.parse_select_core()
            left = A.SetOperation(op=op, left=left, right=right, all=is_all)
        return left

    def parse_select_core(self) -> A.SelectStatement:
        self.expect(KEYWORD, "SELECT")
        distinct = self.match(KEYWORD, "DISTINCT")
        select_items = self.parse_select_list()

        self.expect(KEYWORD, "FROM")
        from_items = self.parse_from()

        where = None
        if self.match(KEYWORD, "WHERE"):
            where = self.parse_condition()

        group_by: List[A.Column] = []
        having = None
        if self.at_keyword("GROUP"):
            self.advance()
            self.expect(KEYWORD, "BY")
            group_by = self.parse_column_list()
            if self.match(KEYWORD, "HAVING"):
                having = self.parse_condition()

        order_by: List[A.OrderItem] = []
        if self.at_keyword("ORDER"):
            self.advance()
            self.expect(KEYWORD, "BY")
            order_by = self.parse_order_list()

        limit=None
        offset=None
        if self.match(KEYWORD, "LIMIT"):
            limit = int(self.expect(NUMBER).value)
            if self.match(KEYWORD, "OFFSET"):
                offset = int(self.expect(NUMBER).value)


        return A.SelectStatement(
            select_items=select_items,
            from_items=from_items,
            distinct=distinct,
            where=where,
            group_by=group_by,
            having=having,
            order_by=order_by,
            limit=limit,
            offset=offset
        )

    def parse_select_list(self) -> List[A.SelectItem]:
        ajtems = [self.parse_select_item()]
        while self.match(COMMA):
            ajtems.append(self.parse_select_item())
        return ajtems

    def parse_select_item(self) -> A.SelectItem:
        if self.at(STAR):
            self.advance()
            return A.SelectItem(expr=A.Star())
        if self.at(IDENT) and self.peek(1).type == DOT and self.peek(2).type == STAR:
            tab = self.advance().value
            self.advance(); self.advance()
            return A.SelectItem(expr=A.Star(table=tab))
        expr = self.parse_value_expr()
        alias = None
        if self.match(KEYWORD, "AS"):
            alias = self.expect(IDENT).value
        return A.SelectItem(expr=expr, alias=alias)

    def parse_value_expr(self):
        if self.at(KEYWORD) and self.peek().value in AGGREGATE_FUNCS:
            return self.parse_aggregate()
        return self.parse_column()

    def parse_aggregate(self) -> A.Aggregate:
        func = self.advance().value
        self.expect(LPAREN)
        distinct = self.match(KEYWORD, "DISTINCT")
        if self.at(STAR):
            self.advance()
            arg = None
        else:
            arg = self.parse_column()
        self.expect(RPAREN)
        return A.Aggregate(func=func, arg=arg, distinct=distinct)

    def parse_from(self) -> List[A.TableRef]:
        tables = [self.parse_table_ref()]
        while self.match(COMMA):
            tables.append(self.parse_table_ref())
        if self.at_keyword("JOIN", "INNER", "LEFT", "RIGHT", "FULL", "OUTER"):
            self.error("JOIN operator nije deo podržane gramatike; "
                       "navedite tabele razdvojene zarezima")
        return tables

    def parse_table_ref(self) -> A.TableRef:
        name = self.expect(IDENT).value
        alias = None
        if self.match(KEYWORD, "AS"):
            alias = self.expect(IDENT).value
        elif self.at(IDENT):
            alias = self.advance().value
        return A.TableRef(name=name, alias=alias)

    def parse_column_list(self) -> List[A.Column]:
        cols = [self.parse_column()]
        while self.match(COMMA):
            cols.append(self.parse_column())
        return cols

    def parse_order_list(self) -> List[A.OrderItem]:
        items = []
        while True:
            col = self.parse_column()
            ascending = not self.match(KEYWORD, "DESC")
            if ascending:
                self.match(KEYWORD, "ASC")
            items.append(A.OrderItem(column=col, ascending=ascending))
            if not self.match(COMMA):
                break
        return items


    def parse_condition(self) -> A.Condition:
        terms = [self.parse_and_term()]
        while self.match(KEYWORD, "OR"):
            terms.append(self.parse_and_term())
        return terms[0] if len(terms) == 1 else A.Or(terms)

    def parse_and_term(self) -> A.Condition:
        terms = [self.parse_factor()]
        while self.match(KEYWORD, "AND"):
            terms.append(self.parse_factor())
        return terms[0] if len(terms) == 1 else A.And(terms)

    def parse_factor(self) -> A.Condition:
        if self.match(KEYWORD, "NOT"):
            return A.Not(self.parse_factor())
        if self.match(LPAREN):
            cond = self.parse_condition()
            self.expect(RPAREN)
            return cond
        return self.parse_comparison()

    def parse_comparison(self) -> And | Comparison:
        left = self.parse_operand()
        negated = self.match(KEYWORD, "NOT")
        if self.match(KEYWORD, "LIKE"):
            cmp = Comparison("LIKE", left, self.parse_operand())
            return A.Not(cmp) if negated else cmp
        if self.match("KEYWORD","IN"):
            self.expect(LPAREN)
            op="<>"if negated else "="
            join=A.And if negated else A.Or
            terms=[Comparison(op,left,self.parse_operand())]
            while(self.match(COMMA)):
                terms.append(Comparison(op,left,self.parse_operand()))
            self.expect(RPAREN)
            return terms[0] if len(terms) == 1 else join(terms)
        if self.match(KEYWORD, "BETWEEN"):
            lo = self.parse_operand()
            self.expect(KEYWORD, "AND")
            hi = self.parse_operand()
            between = A.And([A.Comparison(">=", left, lo), A.Comparison("<=", left, hi)])
            return A.Not(between) if negated else between

        if not self.at(OP):
            self.error("očekivan operator poređenja (=, <>, <, <=, >, >=)")
        op = self.advance().value
        right = self.parse_operand()
        return A.Comparison(op=op, left=left, right=right)

    def parse_operand(self):
        t = self.peek()
        if t.type == NUMBER:
            self.advance()
            if "." in t.value:
                return A.Literal(value=float(t.value), type="DOUBLE")
            return A.Literal(value=int(t.value), type="INT")
        if t.type == STRING:
            self.advance()
            return A.Literal(value=t.value, type="STRING")
        if self.at_keyword("TRUE", "FALSE"):
            self.advance()
            return A.Literal(value=(t.value == "TRUE"), type="BOOL")
        if t.type == KEYWORD and t.value in AGGREGATE_FUNCS:
            return self.parse_aggregate()
        if t.type == IDENT:
            return self.parse_column()
        self.error("očekivana kolona, broj, string ili agregat")

    def parse_column(self) -> A.Column:
        first = self.expect(IDENT).value
        if self.match(DOT):
            return A.Column(name=self.expect(IDENT).value, table=first)
        return A.Column(name=first)
