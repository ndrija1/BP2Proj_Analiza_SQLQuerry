from __future__ import annotations

import itertools
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from . import ast_nodes as A
from . import physical as P
from . import statistics as S
from .catalog import Catalog, Table


SAMO_LEVO_DUBOKA = False

KORISTI_POHLEPNI_REDOSLED = False

PRESKOCI_SORT_AKO_UREDJEN = False

BIRAJ_NAJGORI = False

FORSIRAJ_SPAJANJE: Optional[str] = None

PROJEKCIJA_NANIZE = False


def optimize(query: A.Query, catalog: Catalog) -> P.PlanNode:
    """Napravi najbolji plan za ceo upit. Skupovni upiti (UNION/...) se optimizuju
    rekurzivno - svaka strana posebno, pa se spoje."""
    M = catalog.buffer_blocks
    if isinstance(query, A.SetOperation):
        left = optimize(query.left, catalog)
        right = optimize(query.right, catalog)
        return P.set_operation(left, right, query.op, query.all, M)
    return _optimize_select(query, catalog, M)


def _optimize_select(stmt: A.SelectStatement, catalog: Catalog, M: int) -> P.PlanNode:
    relations = {ref.binding: catalog.table(ref.name) for ref in stmt.from_items}
    bindings = list(relations)

    single, join_preds, constant_preds = _razvrstaj_uslove(stmt.where, bindings)

    dp: Dict[FrozenSet[str], P.PlanNode] = {}
    for b in bindings:
        dp[frozenset({b})] = _pristupni_put(b, single[b], relations, stmt, M)

    all_set = frozenset(bindings)
    if len(bindings) == 1:
        plan = dp[all_set]
    elif KORISTI_POHLEPNI_REDOSLED:
        plan = _pohlepni_redosled(dp, bindings, join_preds, relations, M)
    else:
        _dp_redosled_spajanja(dp, bindings, join_preds, relations, M)
        plan = dp[all_set]

    plan = _zavrsni_deo(plan, stmt, relations, constant_preds, M)
    return plan


def _razvrstaj_uslove(where: Optional[A.Condition], bindings: List[str]):
    """Podeli WHERE na: {tabela: [selekcije]}, [(skup_tabela, uslov_spajanja)],
    [konstantni_uslovi]."""
    single: Dict[str, List[A.Condition]] = {b: [] for b in bindings}
    join_preds: List[Tuple[FrozenSet[str], A.Condition]] = []
    constant_preds: List[A.Condition] = []

    for conj in _split_conjuncts(where):
        dodirnute = _tables_touched(conj) & set(bindings)
        if len(dodirnute) == 0:
            constant_preds.append(conj)
        elif len(dodirnute) == 1:
            jedina = next(iter(dodirnute))
            single[jedina].append(conj)
        else:
            join_preds.append((frozenset(dodirnute), conj))

    return single, join_preds, constant_preds


def _pristupni_put(binding: str, selekcije: List[A.Condition],
                   relations: Dict[str, Table], stmt: A.SelectStatement,
                   M: int) -> P.PlanNode:
    """Najjeftiniji nacin citanja jedne tabele (uz njene selekcije)."""
    table = relations[binding]
    cond = _and(selekcije)

    if cond is None:
        node = P.leaf(table, binding)
    else:
        trazene = _potrebne_kolone_tabele(stmt, binding)
        cands = P.selection_candidates(table, binding, cond, M, trazene)
        node = _izaberi_cvor(cands, kljuc=lambda n: n.processing_cost)
        node.alternatives = _spisak_alternativa(
            cands, kljuc=lambda n: n.processing_cost)

    if PROJEKCIJA_NANIZE:
        node = _projektuj_ranije(node, binding, stmt, M)
    return node


def _dp_redosled_spajanja(dp, bindings, join_preds, relations, M) -> None:
    """Dinamicko programiranje: dp[skup tabela] = najjeftiniji plan koji spaja
    bas te tabele. Gradimo od manjih skupova ka vecim."""
    for size in range(2, len(bindings) + 1):
        for subset in map(frozenset, itertools.combinations(bindings, size)):
            best: Optional[P.PlanNode] = None
            for left_set in _proper_subsets(subset):
                right_set = subset - left_set

                if SAMO_LEVO_DUBOKA and len(left_set) != 1 and len(right_set) != 1:
                    continue
                if left_set not in dp or right_set not in dp:
                    continue
                if min(left_set) > min(right_set):
                    continue

                preds = _preds_between(left_set, right_set, join_preds)
                cand = _best_join(dp[left_set], dp[right_set], left_set,
                                  right_set, preds, relations, M)
                if best is None or _bolji(cand, best):
                    best = cand
            if best is not None:
                dp[subset] = best


def _pohlepni_redosled(dp, bindings, join_preds, relations, M) -> P.PlanNode:
    """MODIFIKACIJA B: pohlepni redosled spajanja. Krecemo od pojedinacnih
    tabela i uvek spajamo trenutno najjeftiniji (povezani) par, dok ne ostane
    jedna komponenta."""
    komponente: List[Tuple[FrozenSet[str], P.PlanNode]] = [
        (frozenset({b}), dp[frozenset({b})]) for b in bindings
    ]

    while len(komponente) > 1:
        najbolji = None
        for i in range(len(komponente)):
            for j in range(i + 1, len(komponente)):
                ls, lnode = komponente[i]
                rs, rnode = komponente[j]
                preds = _preds_between(ls, rs, join_preds)
                cvor = _best_join(lnode, rnode, ls, rs, preds, relations, M)
                cena = P.plan_cost(cvor, False)
                povezani = 0 if preds else 1
                kljuc = (povezani, cena)
                if najbolji is None or kljuc < najbolji[0]:
                    najbolji = (kljuc, i, j, cvor, ls | rs)

        _, i, j, cvor, spojeni = najbolji
        komponente.pop(j)
        komponente.pop(i)
        komponente.append((spojeni, cvor))

    return komponente[0][1]


def _best_join(left: P.PlanNode, right: P.PlanNode,
               left_set: FrozenSet[str], right_set: FrozenSet[str],
               preds: List[A.Condition], relations: Dict[str, Table],
               M: int) -> P.PlanNode:
    """Probaj sve algoritme spajanja za jedan par (leva, desna) i vrati
    najjeftiniji (ili najskuplji ako je ukljucena MODIFIKACIJA D)."""
    cond = _and(preds)
    candidates: List[P.PlanNode] = []

    for outer, inner in ((left, right), (right, left)):
        candidates.append(P.join_block_nested_loop(outer, inner, cond, M))
        candidates.append(P.join_nested_loop(outer, inner, cond, M))

    pairs = _equijoin_pairs(cond, left_set, right_set)
    if pairs:
        candidates.append(P.join_hash(left, right, cond, M))
        candidates.extend(_merge_i_indeks_kandidati(
            left, right, left_set, right_set, cond, pairs, relations, M))

    pool = _primeni_hint_spajanja(candidates)

    chosen = _izaberi_cvor(pool, kljuc=lambda n: P.plan_cost(n, False))
    chosen.alternatives = _spisak_alternativa(
        candidates, kljuc=lambda n: P.plan_cost(n, False))
    return chosen


def _merge_i_indeks_kandidati(left, right, left_set, right_set, cond, pairs,
                              relations, M) -> List[P.PlanNode]:
    """Objedinjeno spajanje + indeks ugnezdjena petlja (traze ekvi-par)."""
    out: List[P.PlanNode] = []
    l_col, r_col = pairs[0]

    l_sorted = _leaf_sorted_on(left, l_col, left_set, relations)
    r_sorted = _leaf_sorted_on(right, r_col, right_set, relations)
    out.append(P.join_merge(left, right, cond, M,
                            str(l_col), str(r_col), l_sorted, r_sorted))

    smerovi = (
        (left, left_set, right, right_set, r_col),
        (right, right_set, left, left_set, l_col),
    )
    for outer, outer_set, inner, inner_set, key in smerovi:
        probe = _inner_probe(inner, inner_set, key, relations)
        if probe is not None:
            cost, desc = probe
            out.append(P.join_indexed_nested_loop(outer, inner, cond, M, cost, desc))
    return out


def _primeni_hint_spajanja(candidates: List[P.PlanNode]) -> List[P.PlanNode]:
    """Ako je FORSIRAJ_SPAJANJE zadat, zadrzi samo kandidate ciji naziv sadrzi
    taj tekst (ako ijedan postoji). Inace vrati sve."""
    if not FORSIRAJ_SPAJANJE:
        return candidates
    forsirani = [c for c in candidates if FORSIRAJ_SPAJANJE in c.algorithm]
    return forsirani or candidates


def _inner_probe(inner, inner_set, key_col, relations):
    """(cena, opis) jedne pretrage indeksa ako je `inner` bazna tabela sa
    indeksom na kljucu spajanja; inace None."""
    if inner.op != "Relation" or len(inner_set) != 1:
        return None
    table = relations[next(iter(inner_set))]
    if key_col.table != next(iter(inner_set)) or not table.has_attribute(key_col.name):
        return None
    return P.index_lookup_cost(table, key_col.name)


def _zavrsni_deo(plan: P.PlanNode, stmt: A.SelectStatement,
                 relations: Dict[str, Table],
                 constant_preds: List[A.Condition], M: int) -> P.PlanNode:
    """Nadogradi na plan: konstantni filter, agregaciju, projekciju, DISTINCT,
    ORDER BY i LIMIT/OFFSET - tim redom."""
    if constant_preds:
        plan = _filter_scan(plan, _and(constant_preds), M)

    order_keys = [str(o.column) for o in stmt.order_by]
    sorted_early = False

    if stmt.has_aggregation:
        plan = _dodaj_agregaciju(plan, stmt, relations, M)
    else:
        plan, sorted_early = _projekcija_i_rani_sort(plan, stmt, order_keys, M)

    if stmt.distinct:
        plan = P.distinct(plan, M)

    if order_keys and not sorted_early:
        if not (PRESKOCI_SORT_AKO_UREDJEN and _vec_uredjen(order_keys, relations)):
            plan = P.sort(plan, order_keys, M)

    if plan.op == "Relation":
        plan = P.full_scan(plan)

    if stmt.offset is not None:
        plan.stats.n_rows = max(1.0, plan.stats.n_rows - stmt.offset)
    if stmt.limit is not None:
        plan.stats.n_rows = min(plan.stats.n_rows, float(stmt.limit))
    return plan


def _dodaj_agregaciju(plan, stmt, relations, M) -> P.PlanNode:
    """Agregacija (+ HAVING) na vrh plana."""
    group_keys = [_key(c) for c in stmt.group_by]
    out_attrs = len(stmt.group_by) + len(stmt.aggregates)
    has_distinct = any(a.distinct for a in stmt.aggregates)
    ulaz_uredjen = _vec_uredjen(group_keys, relations)

    plan = P.aggregate(plan, group_keys, max(1, out_attrs), M,
                       distinct_agg=has_distinct, ulaz_uredjen=ulaz_uredjen)
    if stmt.having is not None:
        plan = _filter_scan(plan, stmt.having, M, label="HAVING")
    return plan


def _projekcija_i_rani_sort(plan, stmt, order_keys, M):
    """Bez agregacije: odluci da li sort ide PRE ili POSLE projekcije, pa
    projektuj. Vraca (plan, da_li_je_vec_sortirano)."""
    select_keys = {_key(it.expr) for it in stmt.select_items
                   if isinstance(it.expr, A.Column)}

    mora_pre = (order_keys and not stmt.is_select_star
                and any(k not in select_keys for k in order_keys))
    if mora_pre:
        plan = P.sort(plan, order_keys, M)
        plan = _project_if_needed(stmt, plan, M)
        return plan, True

    plan = _project_if_needed(stmt, plan, M)
    return plan, False


def _izaberi_cvor(cvorovi: List[P.PlanNode], kljuc) -> P.PlanNode:
    """Vrati najjeftiniji cvor po kljucu `kljuc` (ili najskuplji ako je ukljucen
    prekidac BIRAJ_NAJGORI). Zamena za goli min(...)/max(...) radi citljivosti."""
    if BIRAJ_NAJGORI:
        return max(cvorovi, key=kljuc)
    return min(cvorovi, key=kljuc)


def _bolji(a: P.PlanNode, b: P.PlanNode) -> bool:
    """Da li je plan `a` bolji izbor od `b` (jeftiniji, ili skuplji pod
    BIRAJ_NAJGORI). Poredjenje je uvek nad potpuno materijalizovanom cenom."""
    ca, cb = P.plan_cost(a, False), P.plan_cost(b, False)
    return ca > cb if BIRAJ_NAJGORI else ca < cb


def _spisak_alternativa(cvorovi: List[P.PlanNode], kljuc) -> List[str]:
    """Lista "algoritam: cena" svih kandidata, sortirana od najjeftinijeg."""
    return [f"{c.algorithm}: {kljuc(c)}" for c in sorted(cvorovi, key=kljuc)]


def _potrebne_kolone_tabele(stmt: A.SelectStatement,
                            binding: str) -> Optional[Set[str]]:
    """Skup naziva kolona date tabele koji se koriste bilo gde u upitu
    (SELECT, WHERE, GROUP BY, HAVING, ORDER BY). None ako je SELECT * (tada
    treba sve kolone, pa nema smisla pokrivajuci indeks / projekcija nanize)."""
    if stmt.is_select_star:
        return None

    kolone: Set[str] = set()

    def dodaj(col: A.Column) -> None:
        if col.table == binding:
            kolone.add(col.name)

    for it in stmt.select_items:
        if isinstance(it.expr, A.Column):
            dodaj(it.expr)
        elif isinstance(it.expr, A.Aggregate) and it.expr.arg is not None:
            dodaj(it.expr.arg)
    for c in A.columns_in_condition(stmt.where):
        dodaj(c)
    for c in A.columns_in_condition(stmt.having):
        dodaj(c)
    for c in stmt.group_by:
        dodaj(c)
    for o in stmt.order_by:
        dodaj(o.column)

    return kolone


def _projektuj_ranije(node: P.PlanNode, binding: str,
                      stmt: A.SelectStatement, M: int) -> P.PlanNode:
    """MODIFIKACIJA F: umetni ranu projekciju iznad pristupa tabeli, da kroz
    spajanja putuju samo potrebne kolone."""
    potrebne = _potrebne_kolone_tabele(stmt, binding)
    if not potrebne:
        return node
    kljucevi = [f"{binding}.{ime}" for ime in sorted(potrebne)]
    if len(kljucevi) >= node.stats.n_attrs:
        return node
    return P.project(node, kljucevi, M)


def _vec_uredjen(keys: List[str], relations: Dict[str, Table]) -> bool:
    """Heuristika: da li je (jednotabelarni) rezultat vec sortiran po `keys`?
    Tacno kad postoji jedna tabela i ona ima sortirajuci indeks po tom atributu.
    Koristi je nekoliko modifikacija (interesting orders, free grouping)."""
    if len(keys) != 1 or len(relations) != 1:
        return False
    table = next(iter(relations.values()))
    key = keys[0]
    attr = key.split(".", 1)[1] if "." in key else key
    return table.is_sorted_on(attr)


def _filter_scan(plan, cond, M, label="preostali filter") -> P.PlanNode:
    """Jedan linearni prolaz koji filtrira vec materijalizovan rezultat."""
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


def _split_conjuncts(cond: Optional[A.Condition]) -> List[A.Condition]:
    """Rastavi uslov na listu konjunkata (poravna ugnezdjene AND-ove)."""
    if cond is None:
        return []
    if isinstance(cond, A.And):
        out: List[A.Condition] = []
        for t in cond.terms:
            out.extend(_split_conjuncts(t))
        return out
    return [cond]


def _and(conds: List[A.Condition]) -> Optional[A.Condition]:
    """Spoji listu uslova u jedan AND (None ako je prazna, sam uslov ako je 1)."""
    conds = [c for c in conds if c is not None]
    if not conds:
        return None
    return conds[0] if len(conds) == 1 else A.And(list(conds))


def _tables_touched(cond: A.Condition) -> set:
    """Skup bindinga tabela koje se pominju u uslovu."""
    return {c.table for c in A.columns_in_condition(cond) if c.table is not None}


def _preds_between(left_set, right_set, join_preds) -> List[A.Condition]:
    """Uslovi spajanja koji povezuju bas levu i desnu stranu (primenjuju se
    tacno jednom - kad se te dve strane spoje)."""
    union = left_set | right_set
    out = []
    for bs, cond in join_preds:
        if bs <= union and (bs & left_set) and (bs & right_set):
            out.append(cond)
    return out


def _equijoin_pairs(cond, left_set, right_set) -> List[Tuple[A.Column, A.Column]]:
    """Parovi kolona vezani JEDNAKOSCU preko granice leve i desne strane.
    Gledamo samo konjunkte na vrhu - OR na vrhu ne daje par, pa disjunktivno
    spajanje automatski pada na petlju (jedini algoritam koji radi za OR)."""
    pairs: List[Tuple[A.Column, A.Column]] = []
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
    """Da li je `node` bazna tabela fizicki sortirana po kljucu spajanja
    (pa sort-merge preskace njeno sortiranje)."""
    if node.op != "Relation" or len(node_set) != 1:
        return False
    table = relations[next(iter(node_set))]
    return table.has_attribute(key_col.name) and table.is_sorted_on(key_col.name)


def _proper_subsets(s: FrozenSet[str]):
    """Svi pravi neprazni podskupovi skupa `s` (za deljenje kod DP-a)."""
    elems = list(s)
    for r in range(1, len(elems)):
        for combo in itertools.combinations(elems, r):
            yield frozenset(combo)


def _project_if_needed(stmt: A.SelectStatement, plan: P.PlanNode, M: int) -> P.PlanNode:
    """Dodaj projekciju osim ako je SELECT * ili nema prostih kolona."""
    if stmt.is_select_star:
        return plan
    kept = [_key(it.expr) for it in stmt.select_items if isinstance(it.expr, A.Column)]
    if not kept:
        return plan
    return P.project(plan, kept, M)


def _key(col: A.Column) -> str:
    """Kljuc kolone "binding.naziv" (isti format kao u statistici)."""
    return f"{col.table}.{col.name}"


def objasni_plan(plan: P.PlanNode) -> str:
    """Ceo plan, cvor po cvor, sa objasnjenjem cene i alternativa."""
    linije: List[str] = [f"UKUPNA CENA (materijalizacija) = {P.plan_cost(plan)} "
                         f"blok-transfera", ""]
    for i, cvor in enumerate(P.nabroji_cvorove(plan), 1):
        linije.append(f"({i}) " + P.objasni_cvor(cvor).replace("\n", "\n    "))
        linije.append("")
    return "\n".join(linije)


def uporedi_pristupne_puteve(table: Table, binding: str, cond: A.Condition,
                             M: int) -> str:
    """Izlistaj SVE pristupne puteve za jednu selekciju sa cenama, sortirane od
    najjeftinijeg. Odgovor na pitanje "zasto bas taj indeks?"."""
    cands = P.selection_candidates(table, binding, cond, M)
    cands = sorted(cands, key=lambda n: n.processing_cost)
    linije = [f"Pristupni putevi za: {binding} WHERE {cond}"]
    for c in cands:
        linije.append(f"  {c.processing_cost:>6}  {c.algorithm}")
    linije.append(f"  -> izabran: {cands[0].algorithm} ({cands[0].processing_cost})")
    return "\n".join(linije)
