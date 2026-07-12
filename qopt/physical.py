# Fizicke operacije: gradi cvorove plana (PlanNode) za svaki algoritam,
# sa cenom iz cost_model-a i velicinom izlaza iz statistics-a.
#
# Obracun po modelu materijalizacije: formula vec broji citanje ulaza, pa
# svakom cvoru dodajemo jos upis rezultata (= broj izlaznih blokova).
# Izuzeci: bazne tabele (vec su na disku) i koren plana (rezultat ide
# korisniku) - njihov upis se ne naplacuje. Tako plan sa jednom operacijom
# ostaje jednak vrednosti iz knjige.

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from . import ast_nodes as A
from . import cost_model as C
from . import statistics as S
from .catalog import Table

RANGE_OPS = {"<", "<=", ">", ">="}


@dataclass
class PlanNode:
    op: str                     # interni kljuc operacije ("Join", "Sort"...)
    algorithm: str              # naziv algoritma za prikaz
    stats: S.RelationStats      # procenjena velicina IZLAZA
    processing_cost: int        # cena obrade u blok transferima
    children: List["PlanNode"] = field(default_factory=list)
    details: List[str] = field(default_factory=list)    # formula sa brojevima
    label: str = ""             # uslov / naziv tabele
    materialized: bool = True   # False za bazne tabele
    alternatives: List[str] = field(default_factory=list)  # odbaceni algoritmi

    @property
    def out_blocks(self) -> int:
        return self.stats.blocks

    @property
    def out_rows(self) -> int:
        return self.stats.rows

    def write_cost(self, is_root: bool) -> int:
        if is_root or not self.materialized:
            return 0
        return self.stats.blocks

    def node_cost(self, is_root: bool) -> int:
        return self.processing_cost + self.write_cost(is_root)


def plan_cost(node: PlanNode, is_root: bool = True) -> int:
    """Ukupna cena podstabla: cvor + sva deca, rekurzivno."""
    total = node.node_cost(is_root)
    for ch in node.children:
        total += plan_cost(ch, is_root=False)
    return total


# ---- listovi ------------------------------------------------------------

def leaf(table: Table, binding: str) -> PlanNode:
    # bazna tabela vec lezi na disku, sama po sebi ne kosta nista
    stats = S.base_table_stats(table, binding)
    return PlanNode(
        op="Relation",
        algorithm="(bazna tabela na disku)",
        stats=stats,
        processing_cost=0,
        materialized=False,
        label=table.name if binding == table.name else f"{table.name} {binding}",
        details=[f"{table.name}: n_r={table.row_count}, b_r={table.block_count}, "
                 f"f_r={table.rows_per_block}"],
    )


def full_scan(child: PlanNode) -> PlanNode:
    # za upite tipa SELECT * FROM tabela - mora bar da se procita
    cost = C.linear_scan_cost(child.out_blocks)
    return PlanNode(
        op="Scan",
        algorithm="linearno pretraživanje",
        stats=child.stats,
        processing_cost=cost,
        children=[child],
        details=[f"linearno pretraživanje: b_r = {child.out_blocks}"],
    )


# ---- selekcija: svi moguci pristupni putevi ------------------------------

def selection_candidates(table: Table, binding: str, cond: A.Condition,
                         M: int) -> List[PlanNode]:
    """Vrati po jedan cvor za svaki razuman nacin citanja tabele uz uslov;
    optimizator posle bira najjeftiniji. Velicina izlaza uvek odgovara CELOM
    uslovu - indeks vodi pristup po jednom delu, ostatak se filtrira u memoriji
    bez dodatnih blok transfera."""
    base = leaf(table, binding)
    out_stats, sel = S.apply_selection(base.stats, cond)
    sel_pct = f"{sel * 100:.3f}%"

    def make(algorithm: str, cost: int, extra: List[str]) -> PlanNode:
        return PlanNode(
            op="Selection",
            algorithm=algorithm,
            stats=out_stats,
            processing_cost=cost,
            children=[base],
            label=str(cond),
            details=extra + [
                f"selektivnost={sel_pct} -> redova {table.row_count} -> "
                f"{out_stats.rows}, izlaznih blokova={out_stats.blocks}"
            ],
        )

    candidates: List[PlanNode] = []

    # linearno citanje radi uvek; za jednakost po kljucu prosek je pola fajla
    if _is_single_key_equality(cond, table):
        cost = C.linear_scan_key_equality_cost(table.block_count)
        candidates.append(make(
            "linearno pretraživanje (zaustavi se na prvom pogotku, ključ =)",
            cost, [f"linearno po ključu: ceil(b_r/2) = ceil({table.block_count}/2)"]))
    else:
        candidates.append(make(
            "linearno pretraživanje", C.linear_scan_cost(table.block_count),
            [f"linearno pretraživanje: b_r = {table.block_count}"]))

    # indeksni putevi imaju smisla samo za konjunkte, ne za OR na vrhu
    if not isinstance(cond, A.Or):
        for cmp in _conjuncts_as_comparisons(cond):
            candidates.extend(_index_paths(table, cmp, make))

    # OR gde je svaki clan indeksirana jednakost -> unija pretraga
    if isinstance(cond, A.Or):
        union = _or_index_union(table, cond, make)
        if union is not None:
            candidates.append(union)

    return _cheapest_per_algorithm(candidates)


def _index_paths(table: Table, cmp: A.Comparison, make) -> List[PlanNode]:
    col = _table_column(cmp, table)
    if col is None:
        return []
    if not (isinstance(cmp.left, A.Literal) or isinstance(cmp.right, A.Literal)):
        return []   # kolona-kolona poredjenje indeks ne pomaze
    attr = table.attribute(col.name)
    out: List[PlanNode] = []

    is_eq = cmp.op == "="
    is_range = cmp.op in RANGE_OPS
    # SC = prosecan broj slogova po vrednosti; za opseg racunamo 1/3 tabele
    SC = max(1, math.ceil(table.row_count / max(attr.distinct_values, 1)))
    matching_rows = SC if is_eq else max(1, math.ceil(table.row_count / 3))
    matching_blocks = max(1, math.ceil(matching_rows / table.rows_per_block))

    for ix in table.indexes_on(col.name):
        if ix.is_hash and is_eq:
            if attr.unique:
                cost = C.hash_equality_key_cost()
                out.append(make(f"heš indeks {ix.name} (=, ključ)", cost,
                                [f"heš ključ: kofa + slog = {cost}"]))
            else:
                cost = C.hash_equality_nonkey_cost(SC)
                out.append(make(f"heš indeks {ix.name} (=, ne-ključ)", cost,
                                [f"heš ne-ključ: kofa + SC = 1 + {SC} = {cost}"]))
        elif ix.is_tree and is_eq:
            if attr.unique:
                cost = C.btree_equality_key_cost(ix.tree_height)
                out.append(make(f"B+-stablo {ix.name} (=, ključ)", cost,
                                [f"stablo ključ: HT + 1 = {ix.tree_height} + 1 = {cost}"]))
            elif ix.clustered:
                cost = C.btree_clustered_equality_cost(ix.tree_height, matching_blocks)
                out.append(make(f"B+-stablo {ix.name} (=, sortirajući)", cost,
                                [f"sortirajući: HT + ceil(SC/f) = {ix.tree_height} + "
                                 f"{matching_blocks} = {cost}"]))
            else:
                cost = C.btree_secondary_equality_cost(ix.tree_height, SC)
                out.append(make(f"B+-stablo {ix.name} (=, sekundarni)", cost,
                                [f"sekundarni: HT + SC = {ix.tree_height} + {SC} = {cost}"]))
        elif ix.is_tree and is_range:
            if ix.clustered:
                cost = C.btree_clustered_range_cost(ix.tree_height, matching_blocks)
                out.append(make(f"B+-stablo {ix.name} (opseg, sortirajući)", cost,
                                [f"sortirajući opseg: HT + ceil(redova/f) = "
                                 f"{ix.tree_height} + {matching_blocks} = {cost}"]))
            else:
                cost = C.btree_secondary_range_cost(ix.tree_height, matching_rows)
                out.append(make(f"B+-stablo {ix.name} (opseg, sekundarni)", cost,
                                [f"sekundarni opseg: HT + redova = {ix.tree_height} + "
                                 f"{matching_rows} = {cost}"]))

    # fajl sortiran po atributu -> moze i binarno pretrazivanje
    if table.is_sorted_on(col.name) and (is_eq or is_range):
        cost = C.binary_search_cost(table.block_count, matching_blocks)
        out.append(make("binarno pretraživanje (datoteka sortirana po atributu)", cost,
                        [f"binarno: ceil(log2(b_r)) + (blokova-1) = "
                         f"ceil(log2({table.block_count})) + {matching_blocks - 1} = {cost}"]))
    return out


def _or_index_union(table: Table, cond: A.Or, make) -> Optional[PlanNode]:
    # OR resavamo unijom pretraga samo ako SVAKI clan ima indeksiranu jednakost
    total = 0
    parts = []
    for term in cond.terms:
        if not isinstance(term, A.Comparison) or term.op != "=":
            return None
        col = _table_column(term, table)
        if col is None:
            return None
        usable = table.indexes_on(col.name)
        if not usable:
            return None
        attr = table.attribute(col.name)
        ix = usable[0]
        if attr.unique and ix.is_hash:
            c = C.hash_equality_key_cost()
        elif attr.unique and ix.is_tree:
            c = C.btree_equality_key_cost(ix.tree_height)
        else:
            SC = max(1, math.ceil(table.row_count / max(attr.distinct_values, 1)))
            c = (C.hash_equality_nonkey_cost(SC) if ix.is_hash
                 else C.btree_secondary_equality_cost(ix.tree_height, SC))
        total += c
        parts.append(f"{ix.name}:{c}")
    return make("unija pretraga po indeksima (disjunkcija)", total,
                ["OR indeksiranih jednakosti: " + " + ".join(parts) + f" = {total}"])


# ---- sortiranje ----------------------------------------------------------

def sort(child: PlanNode, keys: List[str], M: int) -> PlanNode:
    b = child.out_blocks
    cost = C.external_sort_cost(b, M)
    runs = C.sort_initial_runs(b, M)
    passes = C.sort_merge_passes(b, M)
    return PlanNode(
        op="Sort",
        algorithm="spoljno objedinjeno sortiranje",
        stats=child.stats.copy(),
        processing_cost=cost,
        children=[child],
        label="sortiranje po " + ", ".join(keys),
        details=[f"tura=ceil({b}/{M})={runs}, prolaza spajanja={passes}",
                 f"cena=b*(2*prolaza+1)={b}*(2*{passes}+1)={cost}"],
    )


# ---- spajanja ------------------------------------------------------------

def join_nested_loop(left: PlanNode, right: PlanNode, cond, M: int) -> PlanNode:
    out, _ = S.estimate_join(left.stats, right.stats, cond)
    cost = C.nested_loop_join_cost(left.out_blocks, left.out_rows, right.out_blocks)
    return _join_node(
        "spajanje ugnežđenom petljom", out, cost, [left, right], cond,
        f"b_r + n_r*b_s = {left.out_blocks} + {left.out_rows}*{right.out_blocks} = {cost}")


def join_block_nested_loop(left: PlanNode, right: PlanNode, cond, M: int) -> PlanNode:
    out, _ = S.estimate_join(left.stats, right.stats, cond)
    cost = C.block_nested_loop_join_cost(left.out_blocks, right.out_blocks, M)
    chunks = math.ceil(left.out_blocks / max(1, M - 2))
    return _join_node(
        "spajanje blok ugnežđenom petljom", out, cost, [left, right], cond,
        f"b_r + ceil(b_r/(M-2))*b_s = {left.out_blocks} + {chunks}*{right.out_blocks} = {cost}")


def index_lookup_cost(table: Table, attr_name: str) -> Optional[Tuple[int, str]]:
    """Cena JEDNE pretrage attr = ? najboljim indeksom nad tabelom, ili None.
    Ovo je cena po torki kod spajanja indeks ugnezdjenom petljom."""
    attr = table.attribute(attr_name)
    best: Optional[Tuple[int, str]] = None
    SC = max(1, math.ceil(table.row_count / max(attr.distinct_values, 1)))
    mb = max(1, math.ceil(SC / table.rows_per_block))
    for ix in table.indexes_on(attr_name):
        if ix.is_hash:
            cost = (C.hash_equality_key_cost() if attr.unique
                    else C.hash_equality_nonkey_cost(SC))
            desc = f"heš {ix.name}"
        elif attr.unique:
            cost = C.btree_equality_key_cost(ix.tree_height)
            desc = f"B+-stablo {ix.name} (ključ)"
        elif ix.clustered:
            cost = C.btree_clustered_equality_cost(ix.tree_height, mb)
            desc = f"B+-stablo {ix.name} (sortirajući)"
        else:
            cost = C.btree_secondary_equality_cost(ix.tree_height, SC)
            desc = f"B+-stablo {ix.name} (sekundarni)"
        if best is None or cost < best[0]:
            best = (cost, desc)
    return best


def join_indexed_nested_loop(left: PlanNode, right: PlanNode, cond, M: int,
                             probe_cost: int, probe_desc: str) -> PlanNode:
    out, _ = S.estimate_join(left.stats, right.stats, cond)
    cost = C.indexed_nested_loop_join_cost(left.out_blocks, left.out_rows, probe_cost)
    return _join_node(
        "spajanje indeks ugnežđenom petljom", out, cost, [left, right], cond,
        f"b_r + n_r*pretraga = {left.out_blocks} + {left.out_rows}*{probe_cost} = {cost}",
        extra=[f"pretraga unutrašnje: {probe_desc} = {probe_cost}"])


def join_merge(left: PlanNode, right: PlanNode, cond, M: int,
               left_key: str, right_key: str,
               left_sorted: bool, right_sorted: bool) -> PlanNode:
    # strana koja nije sortirana dobija Sort cvor ispod sebe
    lchild = left if left_sorted else sort(left, [left_key], M)
    rchild = right if right_sorted else sort(right, [right_key], M)
    out, _ = S.estimate_join(left.stats, right.stats, cond)
    cost = C.merge_join_cost(lchild.out_blocks, rchild.out_blocks)
    note = []
    if left_sorted:
        note.append("leva već sortirana")
    if right_sorted:
        note.append("desna već sortirana")
    return _join_node(
        "objedinjeno spajanje (sort-merge)", out, cost, [lchild, rchild], cond,
        f"faza spajanja: b_r + b_s = {lchild.out_blocks} + {rchild.out_blocks} = {cost}",
        extra=note)


def join_hash(left: PlanNode, right: PlanNode, cond, M: int) -> PlanNode:
    out, _ = S.estimate_join(left.stats, right.stats, cond)
    cost = C.hash_join_cost(left.out_blocks, right.out_blocks, M)
    recursive = min(left.out_blocks, right.out_blocks) > M * (M - 1)
    formula = (f"rekurzivno particionisanje -> {cost}" if recursive else
               f"3*(b_r+b_s) = 3*({left.out_blocks}+{right.out_blocks}) = {cost}")
    return _join_node("heš spajanje", out, cost, [left, right], cond, formula)


def _join_node(algorithm, out_stats, cost, children, cond, formula, extra=None):
    details = list(extra or []) + [
        formula,
        f"rezultat redova ~= {out_stats.rows}, izlaznih blokova={out_stats.blocks}",
    ]
    return PlanNode(
        op="Join",
        algorithm=algorithm,
        stats=out_stats,
        processing_cost=cost,
        children=children,
        label=str(cond) if cond is not None else "(Dekartov proizvod)",
        details=details,
    )


# ---- projekcija, DISTINCT, agregacija, skupovne operacije ----------------

def project(child: PlanNode, kept_keys: List[str], M: int) -> PlanNode:
    out = S.apply_projection(child.stats, kept_keys, distinct_elim=False)
    return PlanNode(
        op="Projection",
        algorithm="skeniranje, izbacivanje kolona (bez eliminacije duplikata)",
        stats=out,
        processing_cost=C.linear_scan_cost(child.out_blocks),
        children=[child],
        label="projektuj " + ", ".join(kept_keys),
        details=[f"skeniraj ulaz: b = {child.out_blocks}"],
    )


def distinct(child: PlanNode, M: int) -> PlanNode:
    # eliminacija duplikata: jeftinije od sortiranja i hesiranja
    out = S.apply_distinct(child.stats)
    b = child.out_blocks
    sort_cost = C.external_sort_cost(b, M)
    hash_cost = b if b <= M else 3 * b
    if sort_cost <= hash_cost:
        algo = "eliminacija duplikata sortiranjem"
        cost, detail = sort_cost, f"spoljno sortiranje pa izbaci susedne duplikate: {sort_cost}"
    else:
        algo = "eliminacija duplikata heširanjem"
        cost, detail = hash_cost, f"heš particije + eliminacija: {hash_cost}"
    return PlanNode(
        op="Distinct", algorithm=algo, stats=out, processing_cost=cost,
        children=[child], details=[detail, f"redova {child.out_rows} -> {out.rows}"],
    )


def aggregate(child: PlanNode, group_keys: List[str], out_attrs: int,
              M: int) -> PlanNode:
    out = S.apply_aggregation(child.stats, group_keys, out_attrs)
    b = child.out_blocks
    sort_cost = C.external_sort_cost(b, M)
    hash_cost = b if out.blocks <= M else 3 * b
    if hash_cost <= sort_cost:
        algo = "agregacija zasnovana na heširanju"
        cost, detail = hash_cost, f"izgradi heš tabelu po ključu grupisanja, jedan prolaz: {hash_cost}"
    else:
        algo = "agregacija zasnovana na sortiranju"
        cost, detail = sort_cost, f"spoljno sortiranje po ključu grupisanja pa agregacija: {sort_cost}"
    label = ("grupisanje po " + ", ".join(group_keys)) if group_keys else "skalarna agregacija"
    return PlanNode(
        op="Aggregation", algorithm=algo, stats=out, processing_cost=cost,
        children=[child], label=label, details=[detail, f"grupa ~= {out.rows}"],
    )


def set_operation(left: PlanNode, right: PlanNode, op: str, keep_all: bool,
                  M: int) -> PlanNode:
    # sortiraj obe strane pa ih spoji jednim prolazom
    lsorted = sort(left, ["sve kolone"], M)
    rsorted = sort(right, ["sve kolone"], M)
    out = S.set_operation_stats(op, left.stats, right.stats, keep_all)
    cost = lsorted.out_blocks + rsorted.out_blocks
    kw = op + (" ALL" if keep_all else "")
    return PlanNode(
        op="SetOp",
        algorithm=f"{kw} objedinjenim spajanjem",
        stats=out,
        processing_cost=cost,
        children=[lsorted, rsorted],
        label=kw,
        details=[f"spoji dva sortirana ulaza: b_l + b_r = "
                 f"{lsorted.out_blocks} + {rsorted.out_blocks} = {cost}",
                 f"rezultat redova ~= {out.rows}"],
    )


# ---- sitne pomocne funkcije ----------------------------------------------

def _table_column(cmp: A.Comparison, table: Table) -> Optional[A.Column]:
    for operand in (cmp.left, cmp.right):
        if isinstance(operand, A.Column) and table.has_attribute(operand.name):
            return operand
    return None


def _conjuncts_as_comparisons(cond: A.Condition) -> List[A.Comparison]:
    if isinstance(cond, A.Comparison):
        return [cond]
    if isinstance(cond, A.And):
        return [t for t in cond.terms if isinstance(t, A.Comparison)]
    return []


def _is_single_key_equality(cond: A.Condition, table: Table) -> bool:
    if not isinstance(cond, A.Comparison) or cond.op != "=":
        return False
    col = _table_column(cond, table)
    return col is not None and table.attribute(col.name).unique


def _cheapest_per_algorithm(candidates: List[PlanNode]) -> List[PlanNode]:
    # isti algoritam moze nastati vise puta (razlicit vodeci uslov) -
    # zadrzi najjeftiniju varijantu svakog
    best: dict = {}
    for c in candidates:
        if c.algorithm not in best or c.processing_cost < best[c.algorithm].processing_cost:
            best[c.algorithm] = c
    return list(best.values())
