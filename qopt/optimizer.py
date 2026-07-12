# Optimizator: validiran AST -> najjeftiniji fizicki plan (System-R pristup).
#
#   1. WHERE se rastavi na konjunkte
#   2. konjunkt nad 1 tabelom = selekcija (gura se na pristupni put),
#      nad 2+ tabela = uslov spajanja
#   3. za svaku tabelu se bira najjeftiniji pristupni put
#   4. redosled spajanja: dinamicko programiranje nad podskupovima tabela,
#      za svaku podelu se probaju svi algoritmi u oba smera
#   5. na vrh idu agregacija/HAVING/projekcija/DISTINCT/ORDER BY
#
# Planovi se porede potpuno materijalizovani (plan_cost sa is_root=False);
# upis korena se oduzima samo jednom, na kraju, kod ispisa ukupne cene.

from __future__ import annotations

import itertools
from typing import Dict, FrozenSet, List, Optional, Tuple

from . import ast_nodes as A
from . import physical as P
from . import statistics as S
from .catalog import Catalog, Table


def optimize(query: A.Query, catalog: Catalog) -> P.PlanNode:
    M = catalog.buffer_blocks
    if isinstance(query, A.SetOperation):
        left = optimize(query.left, catalog)
        right = optimize(query.right, catalog)
        return P.set_operation(left, right, query.op, query.all, M)
    return _optimize_select(query, catalog, M)


def _optimize_select(stmt: A.SelectStatement, catalog: Catalog, M: int) -> P.PlanNode:
    relations = {ref.binding: catalog.table(ref.name) for ref in stmt.from_items}
    bindings = list(relations)

    # 1+2: podela predikata po broju tabela koje dodiruju
    single: Dict[str, List[A.Condition]] = {b: [] for b in bindings}
    join_preds: List[Tuple[FrozenSet[str], A.Condition]] = []
    constant_preds: List[A.Condition] = []
    for conj in _split_conjuncts(stmt.where):
        touched = _tables_touched(conj) & set(bindings)
        if len(touched) == 0:
            constant_preds.append(conj)
        elif len(touched) == 1:
            single[next(iter(touched))].append(conj)
        else:
            join_preds.append((frozenset(touched), conj))

    # 3: najjeftiniji pristupni put za svaku tabelu
    dp: Dict[FrozenSet[str], P.PlanNode] = {}
    for b in bindings:
        cond = _and(single[b])
        if cond is None:
            dp[frozenset({b})] = P.leaf(relations[b], b)
        else:
            cands = P.selection_candidates(relations[b], b, cond, M)
            chosen = min(cands, key=lambda n: n.processing_cost)
            chosen.alternatives = [
                f"{c.algorithm}: {c.processing_cost}"
                for c in sorted(cands, key=lambda n: n.processing_cost)
            ]
            dp[frozenset({b})] = chosen

    # 4: redosled spajanja - DP od parova ka sve vecim podskupovima
    all_set = frozenset(bindings)
    for size in range(2, len(bindings) + 1):
        for subset in map(frozenset, itertools.combinations(bindings, size)):
            best: Optional[P.PlanNode] = None
            for left_set in _proper_subsets(subset):
                right_set = subset - left_set
                if left_set not in dp or right_set not in dp:
                    continue
                if min(left_set) > min(right_set):
                    continue    # svaku podelu gledamo samo jednom
                preds = _preds_between(left_set, right_set, join_preds)
                cand = _best_join(dp[left_set], dp[right_set], left_set,
                                  right_set, preds, relations, M)
                if best is None or P.plan_cost(cand, False) < P.plan_cost(best, False):
                    best = cand
            if best is not None:
                dp[subset] = best

    plan = dp[all_set]

    # 5: vrh plana
    if constant_preds:
        # uslov bez kolona (npr. 1=1) - ostavljamo ga kao filter da se vidi
        plan = _filter_scan(plan, _and(constant_preds), M)

    order_keys = [str(o.column) for o in stmt.order_by]
    sorted_early = False

    if stmt.has_aggregation:
        group_keys = [_key(c) for c in stmt.group_by]
        out_attrs = len(stmt.group_by) + len(stmt.aggregates)
        plan = P.aggregate(plan, group_keys, max(1, out_attrs), M)
        if stmt.having is not None:
            plan = _filter_scan(plan, stmt.having, M, label="HAVING")
    else:
        # ako ORDER BY atribut ne prezivljava projekciju, sortiraj PRE nje -
        # posle projekcije taj atribut fizicki vise ne postoji u rezultatu.
        # Kad prezivljava, sortiramo posle (uze torke, jeftinije).
        select_keys = {_key(it.expr) for it in stmt.select_items
                       if isinstance(it.expr, A.Column)}
        if (order_keys and not stmt.is_select_star
                and any(k not in select_keys for k in order_keys)):
            plan = P.sort(plan, order_keys, M)
            sorted_early = True
        plan = _project_if_needed(stmt, plan, M)

    if stmt.distinct:
        plan = P.distinct(plan, M)

    if order_keys and not sorted_early:
        plan = P.sort(plan, order_keys, M)

    # SELECT * FROM tabela bez icega - tabela mora bar da se procita
    if plan.op == "Relation":
        plan = P.full_scan(plan)

    return plan


def _best_join(left: P.PlanNode, right: P.PlanNode,
               left_set: FrozenSet[str], right_set: FrozenSet[str],
               preds: List[A.Condition], relations: Dict[str, Table],
               M: int) -> P.PlanNode:
    """Proba sve algoritme spajanja za jednu kombinaciju i vrati najjeftiniji."""
    cond = _and(preds)
    candidates: List[P.PlanNode] = []

    # petlje rade za bilo koji uslov (i disjunkciju i ne-ekvi), u oba smera
    for outer, inner in ((left, right), (right, left)):
        candidates.append(P.join_block_nested_loop(outer, inner, cond, M))
        candidates.append(P.join_nested_loop(outer, inner, cond, M))

    # hes/merge/indeks traze bar jedan par kolona vezan jednakoscu
    pairs = _equijoin_pairs(cond, left_set, right_set)
    if pairs:
        candidates.append(P.join_hash(left, right, cond, M))

        l_col, r_col = pairs[0]
        l_sorted = _leaf_sorted_on(left, l_col, left_set, relations)
        r_sorted = _leaf_sorted_on(right, r_col, right_set, relations)
        candidates.append(P.join_merge(left, right, cond, M,
                                       str(l_col), str(r_col), l_sorted, r_sorted))

        # indeks-petlja: unutrasnja mora biti bazna tabela sa indeksom na kljucu
        for outer, outer_set, inner, inner_set, key in (
            (left, left_set, right, right_set, r_col),
            (right, right_set, left, left_set, l_col),
        ):
            probe = _inner_probe(inner, inner_set, key, relations)
            if probe is not None:
                cost, desc = probe
                candidates.append(P.join_indexed_nested_loop(
                    outer, inner, cond, M, cost, desc))

    chosen = min(candidates, key=lambda n: P.plan_cost(n, False))
    chosen.alternatives = [
        f"{c.algorithm}: {P.plan_cost(c, False)}"
        for c in sorted(candidates, key=lambda n: P.plan_cost(n, False))
    ]
    return chosen


def _inner_probe(inner, inner_set, key_col, relations):
    # (cena, opis) jedne pretrage indeksa ako je inner bazna tabela sa indeksom
    if inner.op != "Relation" or len(inner_set) != 1:
        return None
    table = relations[next(iter(inner_set))]
    if key_col.table != next(iter(inner_set)) or not table.has_attribute(key_col.name):
        return None
    return P.index_lookup_cost(table, key_col.name)


def _project_if_needed(stmt: A.SelectStatement, plan: P.PlanNode, M: int) -> P.PlanNode:
    if stmt.is_select_star:
        return plan
    kept = [_key(it.expr) for it in stmt.select_items if isinstance(it.expr, A.Column)]
    if not kept:
        return plan
    return P.project(plan, kept, M)


def _filter_scan(plan, cond, M, label="preostali filter"):
    # filter nad vec materijalizovanim rezultatom (HAVING, konstantni uslovi):
    # jedan linearni prolaz
    out, sel = S.apply_selection(plan.stats, cond)
    return P.PlanNode(
        op="Selection",
        algorithm=f"{label}: skeniranje i filtriranje u memoriji",
        stats=out,
        processing_cost=plan.out_blocks,
        children=[plan],
        label=str(cond),
        details=[f"skeniraj {plan.out_blocks} blokova, selektivnost~={sel * 100:.2f}%"],
    )


# ---- rad sa predikatima ---------------------------------------------------

def _split_conjuncts(cond: Optional[A.Condition]) -> List[A.Condition]:
    if cond is None:
        return []
    if isinstance(cond, A.And):
        out: List[A.Condition] = []
        for t in cond.terms:
            out.extend(_split_conjuncts(t))
        return out
    return [cond]


def _and(conds: List[A.Condition]) -> Optional[A.Condition]:
    conds = [c for c in conds if c is not None]
    if not conds:
        return None
    return conds[0] if len(conds) == 1 else A.And(list(conds))


def _tables_touched(cond: A.Condition) -> set:
    return {c.table for c in A.columns_in_condition(cond) if c.table is not None}


def _preds_between(left_set, right_set, join_preds):
    # uslovi koji povezuju levu i desnu stranu, primenjuju se tacno jednom
    union = left_set | right_set
    return [cond for bs, cond in join_preds
            if bs <= union and (bs & left_set) and (bs & right_set)]


def _equijoin_pairs(cond, left_set, right_set):
    """Parovi kolona vezani jednakoscu preko granice leve i desne strane.
    Gleda samo konjunkte na vrhu - OR na vrhu ne daje par, pa disjunktivno
    spajanje automatski pada na petlju."""
    pairs = []
    for c in _split_conjuncts(cond):
        if not isinstance(c, A.Comparison) or c.op != "=":
            continue
        if not (isinstance(c.left, A.Column) and isinstance(c.right, A.Column)):
            continue
        lt, rt = c.left.table, c.right.table
        if lt in left_set and rt in right_set:
            pairs.append((c.left, c.right))
        elif lt in right_set and rt in left_set:
            pairs.append((c.right, c.left))
    return pairs


def _leaf_sorted_on(node, key_col, node_set, relations) -> bool:
    # bazna tabela fizicki sortirana po kljucu -> merge preskace sortiranje
    if node.op != "Relation" or len(node_set) != 1:
        return False
    table = relations[next(iter(node_set))]
    return table.has_attribute(key_col.name) and table.is_sorted_on(key_col.name)


def _proper_subsets(s: FrozenSet[str]):
    elems = list(s)
    for r in range(1, len(elems)):
        for combo in itertools.combinations(elems, r):
            yield frozenset(combo)


def _key(col: A.Column) -> str:
    return f"{col.table}.{col.name}"
