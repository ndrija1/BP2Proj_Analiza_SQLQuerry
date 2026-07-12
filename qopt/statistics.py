# Procena velicine rezultata (kardinalnosti) po pravilima iz knjige:
#
#   selekcija:  n * selektivnost
#     A = c   -> 1/V(A)          A <> c -> 1 - 1/V(A)
#     opseg   -> 1/3             (nemamo histograme)
#     AND     -> s1*s2           OR -> s1 + s2 - s1*s2
#   ekvi-spajanje A=B: n_r * n_s / max(V(A), V(B))
#
# Rezultat spajanja ima sire torke, pa mu faktor blokiranja pada:
# 1/f = 1/f_r + 1/f_s, a blokovi su uvek ceil(redova / f).

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import ast_nodes as A
from .catalog import Table


def col_key(col: A.Column) -> str:
    # kljuc kolone u distinct recniku: "binding.naziv"
    return f"{col.table}.{col.name}"


def blocks_for(n_rows: float, f: float) -> int:
    return max(1, math.ceil(n_rows / max(f, 1e-9)))


@dataclass
class RelationStats:
    """Procenjena velicina tabele ili medjurezultata koja putuje uz plan."""

    n_rows: float                       # procenjen broj redova
    f: float                            # redova po bloku
    n_attrs: int                        # broj kolona (za projekciju)
    distinct: Dict[str, float] = field(default_factory=dict)   # kolona -> V

    @property
    def blocks(self) -> int:
        return blocks_for(self.n_rows, self.f)

    @property
    def rows(self) -> int:
        return max(1, round(self.n_rows))

    def V(self, key: str) -> float:
        # ako ne znamo V, najgore je da je svaka vrednost razlicita
        return self.distinct.get(key, self.n_rows)

    def copy(self) -> "RelationStats":
        return RelationStats(self.n_rows, self.f, self.n_attrs, dict(self.distinct))


def base_table_stats(table: Table, binding: str) -> RelationStats:
    distinct = {f"{binding}.{a.name}": float(a.distinct_values)
                for a in table.attributes}
    return RelationStats(
        n_rows=float(table.row_count),
        f=float(table.rows_per_block),
        n_attrs=len(table.attributes),
        distinct=distinct,
    )


# ---- selektivnost -----------------------------------------------------

def selectivity(cond: Optional[A.Condition], vlookup: Callable[[str], float]) -> float:
    """Deo redova koji zadovoljava uslov (0..1). vlookup daje V za kolonu."""
    if cond is None:
        return 1.0

    if isinstance(cond, A.And):
        s = 1.0
        for t in cond.terms:
            s *= selectivity(t, vlookup)
        return _clamp01(s)

    if isinstance(cond, A.Or):
        # verovatnoca da NIJEDAN clan ne pogodi, pa komplement
        prod = 1.0
        for t in cond.terms:
            prod *= (1.0 - selectivity(t, vlookup))
        return _clamp01(1.0 - prod)

    if isinstance(cond, A.Not):
        return _clamp01(1.0 - selectivity(cond.term, vlookup))

    if isinstance(cond, A.Comparison):
        return _comparison_selectivity(cond, vlookup)

    return 1.0


def _comparison_selectivity(cmp: A.Comparison, vlookup) -> float:
    left, right = cmp.left, cmp.right
    is_eq = cmp.op == "="
    is_neq = cmp.op in ("<>", "!=")

    left_col = isinstance(left, A.Column)
    right_col = isinstance(right, A.Column)

    if left_col and right_col:
        # kolona op kolona (npr. uslov spajanja)
        if is_eq:
            return 1.0 / max(vlookup(col_key(left)), vlookup(col_key(right)), 1.0)
        if is_neq:
            return _clamp01(
                1.0 - 1.0 / max(vlookup(col_key(left)), vlookup(col_key(right)), 1.0)
            )
        return 1.0 / 3.0

    if left_col or right_col:
        # kolona op konstanta
        col = left if left_col else right
        v = max(vlookup(col_key(col)), 1.0)
        if is_eq:
            return 1.0 / v
        if is_neq:
            return _clamp01(1.0 - 1.0 / v)
        return 1.0 / 3.0

    return _const_compare(cmp)


def _const_compare(cmp: A.Comparison) -> float:
    # obe strane konstante (npr. 1 = 1) - izracunaj direktno
    try:
        lv, rv = cmp.left.value, cmp.right.value    # type: ignore[attr-defined]
        ops = {
            "=": lambda a, b: a == b, "<>": lambda a, b: a != b,
            "!=": lambda a, b: a != b, "<": lambda a, b: a < b,
            "<=": lambda a, b: a <= b, ">": lambda a, b: a > b,
            ">=": lambda a, b: a >= b,
        }
        return 1.0 if ops[cmp.op](lv, rv) else 0.0
    except Exception:
        return 1.0


def _clamp01(x: float) -> float:
    return min(1.0, max(0.0, x))


# ---- primena operacija na statistiku ----------------------------------

def apply_selection(stats: RelationStats, cond: A.Condition):
    """Nova statistika + selektivnost posle selekcije."""
    sel = selectivity(cond, stats.V)
    n_new = max(1.0, stats.n_rows * sel)

    new = stats.copy()
    new.n_rows = n_new
    # V ne moze biti vece od broja preostalih redova
    for k in list(new.distinct):
        new.distinct[k] = min(new.distinct[k], n_new)
    # posle A = const ostaje jedna vrednost te kolone
    for cmp in _top_level_equalities(cond):
        for operand in (cmp.left, cmp.right):
            if isinstance(operand, A.Column):
                new.distinct[col_key(operand)] = 1.0
    return new, sel


def _top_level_equalities(cond: A.Condition) -> List[A.Comparison]:
    # jednakosti kroz AND-ove; kroz OR ne idemo jer tamo nisu garantovane
    if isinstance(cond, A.Comparison):
        return [cond] if cond.op == "=" else []
    if isinstance(cond, A.And):
        out: List[A.Comparison] = []
        for t in cond.terms:
            out.extend(_top_level_equalities(t))
        return out
    return []


def estimate_join(left: RelationStats, right: RelationStats,
                  cond: Optional[A.Condition]):
    """Statistika rezultata spajanja; cond=None znaci Dekartov proizvod."""
    merged: Dict[str, float] = {}
    merged.update(left.distinct)
    merged.update(right.distinct)

    sel = selectivity(cond, lambda k: merged.get(k, max(left.n_rows, right.n_rows)))
    cross = left.n_rows * right.n_rows
    n_new = max(1.0, min(cross, cross * sel))

    f_join = (left.f * right.f) / (left.f + right.f)    # sire torke, manji f
    new = RelationStats(
        n_rows=n_new,
        f=f_join,
        n_attrs=left.n_attrs + right.n_attrs,
        distinct=dict(merged),
    )
    # kolone ekvi-spajanja posle spajanja imaju isti skup vrednosti
    for cmp in _top_level_equalities(cond) if cond is not None else []:
        if isinstance(cmp.left, A.Column) and isinstance(cmp.right, A.Column):
            lk, rk = col_key(cmp.left), col_key(cmp.right)
            v = min(merged.get(lk, n_new), merged.get(rk, n_new))
            new.distinct[lk] = new.distinct[rk] = v
    for k in list(new.distinct):
        new.distinct[k] = min(new.distinct[k], n_new)
    return new, sel


def apply_projection(stats: RelationStats, kept_keys: List[str],
                     distinct_elim: bool) -> RelationStats:
    """Projekcija: uze torke pa vise staje u blok; uz eliminaciju duplikata
    redova ostaje koliko ima razlicitih kombinacija zadrzanih kolona."""
    kept = max(1, len(kept_keys))
    f_new = stats.f * (stats.n_attrs / kept)

    n_new = stats.n_rows
    if distinct_elim:
        combos = 1.0
        for k in kept_keys:
            combos *= stats.V(k)
        n_new = min(stats.n_rows, combos)

    return RelationStats(
        n_rows=max(1.0, n_new),
        f=f_new,
        n_attrs=kept,
        distinct={k: min(stats.V(k), n_new) for k in kept_keys},
    )


def apply_aggregation(stats: RelationStats, group_keys: List[str],
                      n_out_attrs: int) -> RelationStats:
    """Grupisanje: jedan izlazni red po grupi; bez GROUP BY jedna grupa."""
    if not group_keys:
        n_groups = 1.0
    else:
        n_groups = 1.0
        for k in group_keys:
            n_groups *= stats.V(k)
        n_groups = min(stats.n_rows, n_groups)

    f_new = stats.f * (stats.n_attrs / max(1, n_out_attrs))
    return RelationStats(
        n_rows=max(1.0, n_groups),
        f=f_new,
        n_attrs=n_out_attrs,
        distinct={k: min(stats.V(k), n_groups) for k in group_keys},
    )


def apply_distinct(stats: RelationStats) -> RelationStats:
    combos = 1.0
    for k in stats.distinct:
        combos *= stats.distinct[k]
    n_new = min(stats.n_rows, combos) if stats.distinct else stats.n_rows
    new = stats.copy()
    new.n_rows = max(1.0, n_new)
    return new


def set_operation_stats(op: str, left: RelationStats, right: RelationStats,
                        keep_all: bool) -> RelationStats:
    # bez podataka o preklapanju vrednosti koristimo grube granice
    if op == "UNION":
        n = left.n_rows + right.n_rows      # gornja granica
    elif op == "INTERSECT":
        n = min(left.n_rows, right.n_rows) / 2.0
    else:   # EXCEPT
        n = left.n_rows / 2.0
    return RelationStats(
        n_rows=max(1.0, n),
        f=(left.f + right.f) / 2.0,
        n_attrs=left.n_attrs,
        distinct={},
    )
