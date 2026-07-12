# Semanticka provera: da li upit ima smisla u odnosu na semu.
# Proverava tabele/kolone/tipove, agregate i ogranicenja iz teksta projekta
# (max 4 tabele, WHERE konjunkcija do 6 uslova, ORDER BY jedan atribut).
# Usput svakoj koloni popuni .table pa kasnije faze ne moraju da pogadjaju.

from __future__ import annotations

from typing import Dict, List, Tuple

from . import ast_nodes as A
from .catalog import Catalog, Table, is_numeric, types_comparable
from .errors import SemanticError

MAX_TABLES = 4
MAX_WHERE_CONDITIONS = 6
MAX_ORDER_BY = 1


class Scope:
    """Tabele vidljive u jednom SELECT-u, po bindingu (alijas ili naziv)."""

    def __init__(self) -> None:
        self.bindings: Dict[str, Table] = {}
        self.order: List[str] = []      # redosled iz FROM, zbog poruka

    def add(self, binding: str, table: Table) -> None:
        if binding in self.bindings:
            raise SemanticError(
                f"dupliran naziv/alijas tabele '{binding}' u FROM "
                "(dodelite jednom od njih alijas)"
            )
        self.bindings[binding] = table
        self.order.append(binding)

    def resolve(self, col: A.Column) -> Tuple[str, Table]:
        """Nadji kojoj tabeli kolona pripada i upisi to u col.table."""
        if col.table is not None:
            table = self.bindings.get(col.table)
            if table is None:
                raise SemanticError(
                    f"nepoznata tabela/alijas '{col.table}' u '{col}'. "
                    f"Vidljivo: {', '.join(self.order)}"
                )
            if not table.has_attribute(col.name):
                raise SemanticError(f"tabela '{col.table}' nema kolonu '{col.name}'")
            return col.table, table

        # bez kvalifikatora: kolona mora postojati u tacno jednoj tabeli
        matches = [b for b in self.order if self.bindings[b].has_attribute(col.name)]
        if not matches:
            raise SemanticError(f"nepoznata kolona '{col.name}'")
        if len(matches) > 1:
            raise SemanticError(
                f"dvosmislena kolona '{col.name}' - postoji u "
                f"{', '.join(matches)}; kvalifikujte je (npr. {matches[0]}.{col.name})"
            )
        col.table = matches[0]
        return matches[0], self.bindings[matches[0]]


def analyze(query: A.Query, catalog: Catalog) -> A.Query:
    _analyze_query(query, catalog)
    return query


def _analyze_query(query: A.Query, catalog: Catalog) -> List[Tuple[str, str]]:
    # vraca izlazne kolone kao (naziv, tip) - treba za proveru UNION-a
    if isinstance(query, A.SetOperation):
        left_cols = _analyze_query(query.left, catalog)
        right_cols = _analyze_query(query.right, catalog)
        if len(left_cols) != len(right_cols):
            raise SemanticError(
                f"{query.op}: obe strane moraju imati isti broj kolona "
                f"({len(left_cols)} naspram {len(right_cols)})"
            )
        for i, ((_, lt), (_, rt)) in enumerate(zip(left_cols, right_cols), 1):
            if not types_comparable(lt, rt):
                raise SemanticError(
                    f"{query.op}: kolona {i} ima nesaglasan tip ({lt} naspram {rt})"
                )
        return left_cols
    return _analyze_select(query, catalog)


def _analyze_select(stmt: A.SelectStatement, catalog: Catalog) -> List[Tuple[str, str]]:
    # FROM
    scope = Scope()
    for ref in stmt.from_items:
        scope.add(ref.binding, catalog.table(ref.name))
    if len(scope.order) > MAX_TABLES:
        raise SemanticError(
            f"upit koristi {len(scope.order)} tabela; ograničenje je {MAX_TABLES}"
        )

    # WHERE - broj konjunkta + tipovi; agregati ovde nisu dozvoljeni
    if stmt.where is not None:
        n_conditions = len(_top_level_conjuncts(stmt.where))
        if n_conditions > MAX_WHERE_CONDITIONS:
            raise SemanticError(
                f"WHERE ima {n_conditions} konjugovanih uslova; ograničenje je "
                f"{MAX_WHERE_CONDITIONS}"
            )
        _check_condition(stmt.where, scope, allow_aggregates=False, where="WHERE")

    # GROUP BY
    grouped_keys = set()
    for col in stmt.group_by:
        binding, _ = scope.resolve(col)
        grouped_keys.add((binding, col.name))

    grouping = stmt.has_aggregation

    # SELECT lista
    output: List[Tuple[str, str]] = []
    for item in stmt.select_items:
        output.extend(_check_select_item(item, scope, grouping, grouped_keys))

    # HAVING ima smisla samo uz grupisanje
    if stmt.having is not None:
        if not grouping:
            raise SemanticError("HAVING zahteva GROUP BY ili agregat")
        _check_condition(stmt.having, scope, allow_aggregates=True, where="HAVING")

    # ORDER BY
    if len(stmt.order_by) > MAX_ORDER_BY:
        raise SemanticError(
            f"ORDER BY koristi {len(stmt.order_by)} atributa; ograničenje je "
            f"{MAX_ORDER_BY}"
        )
    for oi in stmt.order_by:
        binding, _ = scope.resolve(oi.column)
        if grouping and (binding, oi.column.name) not in grouped_keys:
            raise SemanticError(
                f"ORDER BY '{oi.column}' uz agregaciju zahteva da atribut "
                f"bude u GROUP BY"
            )

    return output


def _top_level_conjuncts(cond: A.Condition):
    # uslovi vezani AND-om na najvisem nivou; (a OR b) je jedan uslov
    if isinstance(cond, A.And):
        out = []
        for t in cond.terms:
            out.extend(_top_level_conjuncts(t))
        return out
    return [cond]


def _check_select_item(item, scope, grouping, grouped_keys):
    expr = item.expr

    if isinstance(expr, A.Star):
        if grouping:
            raise SemanticError("SELECT * se ne može kombinovati sa agregacijom")
        cols = []
        for binding in scope.order:
            for attr in scope.bindings[binding].attributes:
                cols.append((attr.name, attr.type))
        return cols

    if isinstance(expr, A.Aggregate):
        return [(item.alias or str(expr), _aggregate_type(expr, scope))]

    if isinstance(expr, A.Column):
        binding, table = scope.resolve(expr)
        if grouping and (binding, expr.name) not in grouped_keys:
            raise SemanticError(
                f"kolona '{expr}' mora biti u GROUP BY ili unutar agregata"
            )
        return [(item.alias or expr.name, table.attribute(expr.name).type)]

    raise SemanticError(f"nepodržana stavka u SELECT listi: {expr}")


def _aggregate_type(agg: A.Aggregate, scope: Scope) -> str:
    """Tip rezultata agregata + provera argumenta."""
    if agg.func == "COUNT":
        if agg.arg is not None:
            scope.resolve(agg.arg)
        return "INT"

    if agg.arg is None:
        raise SemanticError(f"{agg.func} zahteva kolonu kao argument, ne *")
    _, table = scope.resolve(agg.arg)
    arg_type = table.attribute(agg.arg.name).type

    if agg.func in ("SUM", "AVG"):
        if not is_numeric(arg_type):
            raise SemanticError(
                f"{agg.func} traži numeričku kolonu, a '{agg.arg}' je {arg_type}"
            )
        return "DOUBLE" if agg.func == "AVG" else arg_type
    return arg_type    # MIN/MAX vracaju tip argumenta


def _check_condition(cond, scope, allow_aggregates, where):
    if isinstance(cond, (A.And, A.Or)):
        for t in cond.terms:
            _check_condition(t, scope, allow_aggregates, where)
    elif isinstance(cond, A.Not):
        _check_condition(cond.term, scope, allow_aggregates, where)
    elif isinstance(cond, A.Comparison):
        _check_comparison(cond, scope, allow_aggregates, where)
    else:
        raise SemanticError(f"nepodržan čvor uslova: {cond}")


def _check_comparison(cmp, scope, allow_aggregates, where):
    lt = _operand_type(cmp.left, scope, allow_aggregates, where)
    rt = _operand_type(cmp.right, scope, allow_aggregates, where)
    lt, rt = _coerce_dates(cmp.left, lt, cmp.right, rt)

    if not types_comparable(lt, rt):
        raise SemanticError(
            f"nesaglasan tip u '{cmp}': ne mogu da poredim {lt} sa {rt}"
        )
    if cmp.op not in ("=", "<>", "!=") and (lt == "BOOL" or rt == "BOOL"):
        raise SemanticError(
            f"ne mogu da koristim '{cmp.op}' nad logičkim vrednostima u '{cmp}'"
        )


def _operand_type(op, scope, allow_aggregates, where):
    if isinstance(op, A.Literal):
        return op.type
    if isinstance(op, A.Column):
        _, table = scope.resolve(op)
        return table.attribute(op.name).type
    if isinstance(op, A.Aggregate):
        if not allow_aggregates:
            raise SemanticError(f"agregat '{op}' nije dozvoljen u {where}")
        return _aggregate_type(op, scope)
    raise SemanticError(f"nepodržan operand: {op}")


def _coerce_dates(left, lt, right, rt):
    # 'datum >= '2024-01-01'': string literal uz DATE kolonu tretiraj kao DATE
    if lt == "DATE" and isinstance(right, A.Literal) and rt == "STRING":
        right.type = "DATE"
        return lt, "DATE"
    if rt == "DATE" and isinstance(left, A.Literal) and lt == "STRING":
        left.type = "DATE"
        return "DATE", rt
    return lt, rt
