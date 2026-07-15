from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Union


@dataclass
class Column:
    """Kolona kako je napisana u upitu; table je alijas/naziv ili None.
    Semanticka analiza kasnije popuni table za nekvalifikovane kolone."""

    name: str
    table: Optional[str] = None

    def __str__(self) -> str:
        return f"{self.table}.{self.name}" if self.table else self.name


@dataclass
class Literal:
    value: object
    type: str

    def __str__(self) -> str:
        if self.type in ("STRING", "DATE"):
            return f"'{self.value}'"
        return str(self.value)


Operand = Union[Column, Literal]


@dataclass
class Aggregate:
    func: str
    arg: Optional[Column]
    distinct: bool = False

    def __str__(self) -> str:
        inner = "*" if self.arg is None else str(self.arg)
        return f"{self.func}({'DISTINCT ' if self.distinct else ''}{inner})"


@dataclass
class Star:
    table: Optional[str] = None

    def __str__(self) -> str:
        return f"{self.table}.*" if self.table else "*"


@dataclass
class SelectItem:
    """Jedna stavka SELECT liste: izraz + opcioni alijas."""

    expr: Union[Column, Aggregate, Star]
    alias: Optional[str] = None

    def __str__(self) -> str:
        return f"{self.expr} AS {self.alias}" if self.alias else str(self.expr)


@dataclass
class Comparison:
    op: str
    left: Operand
    right: Operand

    def __str__(self) -> str:
        return f"{self.left} {self.op} {self.right}"


@dataclass
class And:
    terms: List["Condition"] = field(default_factory=list)

    def __str__(self) -> str:
        return "(" + " AND ".join(str(t) for t in self.terms) + ")"


@dataclass
class Or:
    terms: List["Condition"] = field(default_factory=list)

    def __str__(self) -> str:
        return "(" + " OR ".join(str(t) for t in self.terms) + ")"


@dataclass
class Not:
    term: "Condition"

    def __str__(self) -> str:
        return f"NOT {self.term}"


Condition = Union[Comparison, And, Or, Not]


@dataclass
class TableRef:
    name: str
    alias: Optional[str] = None

    @property
    def binding(self) -> str:
        """Ime kojim se kolone pozivaju na tabelu - alijas ako postoji."""
        return self.alias or self.name

    def __str__(self) -> str:
        return f"{self.name} {self.alias}" if self.alias else self.name


@dataclass
class OrderItem:
    column: Column
    ascending: bool = True

    def __str__(self) -> str:
        return f"{self.column} {'ASC' if self.ascending else 'DESC'}"


@dataclass
class SelectStatement:
    select_items: List[SelectItem]
    from_items: List[TableRef]
    distinct: bool = False
    where: Optional[Condition] = None
    group_by: List[Column] = field(default_factory=list)
    having: Optional[Condition] = None
    order_by: List[OrderItem] = field(default_factory=list)
    limit: Optional[int] = None
    offset: Optional[int] = None

    @property
    def is_select_star(self) -> bool:
        return len(self.select_items) == 1 and isinstance(
            self.select_items[0].expr, Star
        )

    @property
    def aggregates(self) -> List[Aggregate]:
        return [it.expr for it in self.select_items
                if isinstance(it.expr, Aggregate)]

    @property
    def has_aggregation(self) -> bool:
        return bool(self.aggregates) or bool(self.group_by)

    def __str__(self) -> str:
        parts = ["SELECT" + (" DISTINCT" if self.distinct else "")]
        parts.append(", ".join(str(it) for it in self.select_items))
        parts.append("FROM " + ", ".join(str(t) for t in self.from_items))
        if self.where is not None:
            parts.append("WHERE " + str(self.where))
        if self.group_by:
            parts.append("GROUP BY " + ", ".join(str(c) for c in self.group_by))
        if self.having is not None:
            parts.append("HAVING " + str(self.having))
        if self.order_by:
            parts.append("ORDER BY " + ", ".join(str(o) for o in self.order_by))
        return " ".join(parts)


@dataclass
class SetOperation:
    op: str
    left: "Query"
    right: "Query"
    all: bool = False

    def __str__(self) -> str:
        kw = self.op + (" ALL" if self.all else "")
        return f"({self.left})\n{kw}\n({self.right})"


Query = Union[SelectStatement, SetOperation]


def columns_in_condition(cond: Optional[Condition]) -> List[Column]:
    """Pokupi sve kolone iz stabla uslova (rekurzivno)."""
    if cond is None:
        return []
    if isinstance(cond, Comparison):
        return [o for o in (cond.left, cond.right) if isinstance(o, Column)]
    if isinstance(cond, (And, Or)):
        out: List[Column] = []
        for t in cond.terms:
            out.extend(columns_in_condition(t))
        return out
    if isinstance(cond, Not):
        return columns_in_condition(cond.term)
    return []


def iter_comparisons(cond: Optional[Condition]):
    """Generator: svako pojedinacno poredjenje iz stabla uslova."""
    if cond is None:
        return
    if isinstance(cond, Comparison):
        yield cond
    elif isinstance(cond, (And, Or)):
        for t in cond.terms:
            yield from iter_comparisons(t)
    elif isinstance(cond, Not):
        yield from iter_comparisons(cond.term)
