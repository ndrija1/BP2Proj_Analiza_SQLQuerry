from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple

from . import ast_nodes as A
from . import cost_model as C
from . import statistics as S
from .catalog import Table

RANGE_OPS = {"<", "<=", ">", ">="}


PIPELINE = False

SAMO_INDEKS_SCAN = False

GRUPISANJE_BEZ_SORT_AKO_UREDJEN = False


@dataclass
class PlanNode:
    """Jedan cvor u STABLU plana izvrsavanja.

    Listovi su bazne tabele; unutrasnji cvorovi su operacije. Deca cvora su
    njegov ulaz - podaci teku ODOZDO NAGORE. Svaki cvor nosi svoju cenu obrade
    i procenjenu velicinu izlaza, pa optimizator preko `plan_cost` moze da
    uporedi dva cela plana.
    """

    op: str
    algorithm: str
    stats: S.RelationStats
    processing_cost: int
    children: List["PlanNode"] = field(default_factory=list)
    details: List[str] = field(default_factory=list)
    label: str = ""
    materialized: bool = True
    alternatives: List[str] = field(default_factory=list)

    @property
    def out_blocks(self) -> int:
        """Broj blokova koje zauzima izlaz ovog cvora."""
        return self.stats.blocks

    @property
    def out_rows(self) -> int:
        """Broj redova u izlazu ovog cvora."""
        return self.stats.rows

    def write_cost(self, is_root: bool) -> int:
        """Cena UPISA rezultata na disk (materijalizacija) = broj izlaznih
        blokova. Nula u tri slucaja: koren (rezultat ide korisniku), bazna
        tabela (vec na disku), ili ukljucen pipelining."""
        if is_root or not self.materialized or PIPELINE:
            return 0
        return self.stats.blocks

    def node_cost(self, is_root: bool) -> int:
        """Doprinos SAMO ovog cvora = cena obrade (citanje ulaza) + upis izlaza.
        NE ukljucuje decu - njih sabira plan_cost."""
        return self.processing_cost + self.write_cost(is_root)


def plan_cost(node: PlanNode, is_root: bool = True) -> int:
    """Ukupna cena celog podstabla: ovaj cvor + sva deca, rekurzivno.

    is_root=True SAMO za pravi vrh plana (njemu se ne naplacuje upis rezultata).
    Sva deca se racunaju sa is_root=False (uvek placaju materijalizaciju).
    """
    total = node.node_cost(is_root)
    for dete in node.children:
        total += plan_cost(dete, is_root=False)
    return total


def leaf(table: Table, binding: str) -> PlanNode:
    """List plana: bazna tabela kakva lezi na disku. Sama po sebi ne kosta
    nista (nije jos procitana ni obradjena)."""
    stats = S.base_table_stats(table, binding)
    if binding == table.name:
        oznaka = table.name
    else:
        oznaka = f"{table.name} {binding}"

    return PlanNode(
        op="Relation",
        algorithm="(bazna tabela na disku)",
        stats=stats,
        processing_cost=0,
        materialized=False,
        label=oznaka,
        details=[f"{table.name}: n_r={table.row_count}, b_r={table.block_count}, "
                 f"f_r={table.rows_per_block}"],
    )


def full_scan(child: PlanNode) -> PlanNode:
    """Puno linearno citanje tabele - za upite tipa `SELECT * FROM tabela`
    (nema uslova, ali tabela ipak mora bar da se procita)."""
    cost = C.linear_scan_cost(child.out_blocks)
    return PlanNode(
        op="Scan",
        algorithm="linearno pretraživanje",
        stats=child.stats,
        processing_cost=cost,
        children=[child],
        details=[f"linearno pretraživanje: b_r = {child.out_blocks}"],
    )


def selection_candidates(table: Table, binding: str, cond: A.Condition,
                         M: int,
                         trazene_kolone: Optional[Set[str]] = None) -> List[PlanNode]:
    """Vrati po jedan cvor-kandidat za svaki razuman nacin citanja tabele uz
    dati uslov. Optimizator posle bira najjeftiniji.

    Bitno: velicina IZLAZA je ista za sve puteve (zavisi od uslova, ne od nacina
    citanja) - indeks samo vodi pristup po jednom delu uslova, a ostatak se
    filtrira u memoriji bez dodatnih blok-transfera. Razlikuje se samo CENA.

    `trazene_kolone` (opciono) koristi index-only scan modifikacija.
    """
    base = leaf(table, binding)
    out_stats, sel = S.apply_selection(base.stats, cond)
    sel_pct = f"{sel * 100:.3f}%"

    def make(algorithm: str, cost: int, extra: List[str]) -> PlanNode:
        opis_izlaza = (f"selektivnost={sel_pct} -> redova {table.row_count} -> "
                       f"{out_stats.rows}, izlaznih blokova={out_stats.blocks}")
        return PlanNode(
            op="Selection",
            algorithm=algorithm,
            stats=out_stats,
            processing_cost=cost,
            children=[base],
            label=str(cond),
            details=extra + [opis_izlaza],
        )

    candidates: List[PlanNode] = []

    if _is_single_key_equality(cond, table):
        cost = C.linear_scan_key_equality_cost(table.block_count)
        candidates.append(make(
            "linearno pretraživanje (zaustavi se na prvom pogotku, ključ =)",
            cost,
            [f"linearno po ključu: ceil(b_r/2) = ceil({table.block_count}/2)"]))
    else:
        cost = C.linear_scan_cost(table.block_count)
        candidates.append(make(
            "linearno pretraživanje", cost,
            [f"linearno pretraživanje: b_r = {table.block_count}"]))

    if not isinstance(cond, A.Or):
        for cmp in _conjuncts_as_comparisons(cond):
            candidates.extend(_index_paths(table, cmp, make))
            if SAMO_INDEKS_SCAN and trazene_kolone:
                io = _index_only_kandidat(table, cmp, trazene_kolone, make)
                if io is not None:
                    candidates.append(io)

    if isinstance(cond, A.Or):
        union = _or_index_union(table, cond, make)
        if union is not None:
            candidates.append(union)

    return _cheapest_per_algorithm(candidates)


def _index_paths(table: Table, cmp: A.Comparison, make) -> List[PlanNode]:
    """Svi indeksni pristupni putevi za jedno poredjenje `kolona op konstanta`."""
    col = _table_column(cmp, table)
    if col is None:
        return []
    if not (isinstance(cmp.left, A.Literal) or isinstance(cmp.right, A.Literal)):
        return []

    attr = table.attribute(col.name)
    is_eq = cmp.op == "="
    is_range = cmp.op in RANGE_OPS

    SC = max(1, math.ceil(table.row_count / max(attr.distinct_values, 1)))
    matching_rows = SC if is_eq else max(1, math.ceil(table.row_count / 3))
    matching_blocks = max(1, math.ceil(matching_rows / table.rows_per_block))

    out: List[PlanNode] = []
    for ix in table.indexes_on(col.name):
        put = _jedan_indeksni_put(ix, attr, is_eq, is_range,
                                  SC, matching_rows, matching_blocks, make)
        if put is not None:
            out.append(put)

    if table.is_sorted_on(col.name) and (is_eq or is_range):
        cost = C.binary_search_cost(table.block_count, matching_blocks)
        out.append(make(
            "binarno pretraživanje (datoteka sortirana po atributu)", cost,
            [f"binarno: ceil(log2(b_r)) + (blokova-1) = "
             f"ceil(log2({table.block_count})) + {matching_blocks - 1} = {cost}"]))
    return out


def _jedan_indeksni_put(ix, attr, is_eq, is_range, SC, matching_rows,
                        matching_blocks, make) -> Optional[PlanNode]:
    """Napravi kandidat za JEDAN indeks nad datim uslovom, ili None ako taj
    indeks ne pomaze tom uslovu (npr. hes na opseg)."""
    HT = ix.tree_height

    if ix.is_hash and is_eq:
        if attr.unique:
            cost = C.hash_equality_key_cost()
            return make(f"heš indeks {ix.name} (=, ključ)", cost,
                        [f"heš ključ: kofa + slog = {cost}"])
        cost = C.hash_equality_nonkey_cost(SC)
        return make(f"heš indeks {ix.name} (=, ne-ključ)", cost,
                    [f"heš ne-ključ: kofa + SC = 1 + {SC} = {cost}"])

    if ix.is_tree and is_eq:
        if attr.unique:
            cost = C.btree_equality_key_cost(HT)
            return make(f"B+-stablo {ix.name} (=, ključ)", cost,
                        [f"stablo ključ: HT + 1 = {HT} + 1 = {cost}"])
        if ix.clustered:
            cost = C.btree_clustered_equality_cost(HT, matching_blocks)
            return make(f"B+-stablo {ix.name} (=, sortirajući)", cost,
                        [f"sortirajući: HT + ceil(SC/f) = {HT} + {matching_blocks} = {cost}"])
        cost = C.btree_secondary_equality_cost(HT, SC)
        return make(f"B+-stablo {ix.name} (=, sekundarni)", cost,
                    [f"sekundarni: HT + SC = {HT} + {SC} = {cost}"])

    if ix.is_tree and is_range:
        if ix.clustered:
            cost = C.btree_clustered_range_cost(HT, matching_blocks)
            return make(f"B+-stablo {ix.name} (opseg, sortirajući)", cost,
                        [f"sortirajući opseg: HT + ceil(redova/f) = "
                         f"{HT} + {matching_blocks} = {cost}"])
        cost = C.btree_secondary_range_cost(HT, matching_rows)
        return make(f"B+-stablo {ix.name} (opseg, sekundarni)", cost,
                    [f"sekundarni opseg: HT + redova = {HT} + {matching_rows} = {cost}"])

    if ix.is_bitmap and is_eq:
        cost = 1 + matching_blocks
        return make(f"bitmap indeks {ix.name} (=)", cost,
                    [f"bitmap: 1 + {matching_blocks} = {cost}"])

    return None


def _index_only_kandidat(table: Table, cmp: A.Comparison,
                         trazene_kolone: Set[str], make) -> Optional[PlanNode]:
    """MODIFIKACIJA index-only: ako neki indeks krece od kolone iz uslova I
    sadrzi SVE trazene kolone, redove ne moramo da dohvatamo -> jeftinije."""
    col = _table_column(cmp, table)
    if col is None or cmp.op != "=":
        return None
    attr = table.attribute(col.name)
    SC = max(1, math.ceil(table.row_count / max(attr.distinct_values, 1)))
    pogodaka_blokova = max(1, math.ceil(SC / table.rows_per_block))

    for ix in table.indexes_on(col.name):
        if not ix.is_tree:
            continue
        pokriva = trazene_kolone.issubset(set(ix.attributes))
        if not pokriva:
            continue
        cost = ix.tree_height + pogodaka_blokova
        return make(f"B+-stablo {ix.name} (index-only, pokrivajući)", cost,
                    [f"pokrivajući indeks (bez dohvatanja redova): "
                     f"HT + ceil(SC/f) = {ix.tree_height} + {pogodaka_blokova} = {cost}"])
    return None


def _or_index_union(table: Table, cond: A.Or, make) -> Optional[PlanNode]:
    """OR resavamo unijom pretraga - ali samo ako SVAKI clan ima indeksiranu
    jednakost. Inace vracamo None (pa OR pada na linearno/petlju)."""
    total = 0
    delovi: List[str] = []
    for term in cond.terms:
        if not isinstance(term, A.Comparison) or term.op != "=":
            return None
        col = _table_column(term, table)
        if col is None:
            return None
        upotrebljivi = table.indexes_on(col.name)
        if not upotrebljivi:
            return None

        attr = table.attribute(col.name)
        ix = upotrebljivi[0]
        c = _cena_ekvi_pretrage(attr, ix, table)
        total += c
        delovi.append(f"{ix.name}:{c}")

    return make("unija pretraga po indeksima (disjunkcija)", total,
                ["OR indeksiranih jednakosti: " + " + ".join(delovi) + f" = {total}"])


def _cena_ekvi_pretrage(attr, ix, table: Table) -> int:
    """Cena jedne pretrage `atribut = vrednost` datim indeksom (za OR uniju)."""
    if attr.unique and ix.is_hash:
        return C.hash_equality_key_cost()
    if attr.unique and ix.is_tree:
        return C.btree_equality_key_cost(ix.tree_height)
    SC = max(1, math.ceil(table.row_count / max(attr.distinct_values, 1)))
    if ix.is_hash:
        return C.hash_equality_nonkey_cost(SC)
    return C.btree_secondary_equality_cost(ix.tree_height, SC)


def sort(child: PlanNode, keys: List[str], M: int) -> PlanNode:
    """Sortiraj izlaz deteta po datim kljucevima (spoljno objedinjeno
    sortiranje - radi i kad podaci ne staju u bafer)."""
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


def join_nested_loop(left: PlanNode, right: PlanNode, cond, M: int) -> PlanNode:
    """Spajanje ugnezdjenom petljom: za svaki red spoljne relacije procitaj celu
    unutrasnju. Radi za BILO KOJI uslov (i disjunkciju, i ne-ekvi)."""
    out, _ = S.estimate_join(left.stats, right.stats, cond)
    cost = C.nested_loop_join_cost(left.out_blocks, left.out_rows, right.out_blocks)
    formula = (f"b_r + n_r*b_s = {left.out_blocks} + "
               f"{left.out_rows}*{right.out_blocks} = {cost}")
    return _join_node("spajanje ugnežđenom petljom", out, cost, [left, right],
                      cond, formula)


def join_block_nested_loop(left: PlanNode, right: PlanNode, cond, M: int) -> PlanNode:
    """Blok ugnezdjena petlja: spoljnu citamo u komadima od M-2 bloka, pa
    unutrasnju procitamo jednom po komadu (mnogo manje prolaza)."""
    out, _ = S.estimate_join(left.stats, right.stats, cond)
    cost = C.block_nested_loop_join_cost(left.out_blocks, right.out_blocks, M)
    chunks = math.ceil(left.out_blocks / max(1, M - 2))
    formula = (f"b_r + ceil(b_r/(M-2))*b_s = {left.out_blocks} + "
               f"{chunks}*{right.out_blocks} = {cost}")
    return _join_node("spajanje blok ugnežđenom petljom", out, cost,
                      [left, right], cond, formula)


def join_indexed_nested_loop(left: PlanNode, right: PlanNode, cond, M: int,
                             probe_cost: int, probe_desc: str) -> PlanNode:
    """Indeks ugnezdjena petlja: umesto da citamo celu unutrasnju, za svaki red
    spoljne uradimo JEDNU pretragu indeksa nad unutrasnjom."""
    out, _ = S.estimate_join(left.stats, right.stats, cond)
    cost = C.indexed_nested_loop_join_cost(left.out_blocks, left.out_rows, probe_cost)
    formula = (f"b_r + n_r*pretraga = {left.out_blocks} + "
               f"{left.out_rows}*{probe_cost} = {cost}")
    return _join_node("spajanje indeks ugnežđenom petljom", out, cost,
                      [left, right], cond, formula,
                      extra=[f"pretraga unutrašnje: {probe_desc} = {probe_cost}"])


def join_merge(left: PlanNode, right: PlanNode, cond, M: int,
               left_key: str, right_key: str,
               left_sorted: bool, right_sorted: bool) -> PlanNode:
    """Objedinjeno spajanje (sort-merge): oba ulaza moraju biti sortirana po
    kljucu spajanja; strana koja nije - dobija Sort cvor ispod sebe."""
    lchild = left if left_sorted else sort(left, [left_key], M)
    rchild = right if right_sorted else sort(right, [right_key], M)
    out, _ = S.estimate_join(left.stats, right.stats, cond)
    cost = C.merge_join_cost(lchild.out_blocks, rchild.out_blocks)

    napomene: List[str] = []
    if left_sorted:
        napomene.append("leva već sortirana")
    if right_sorted:
        napomene.append("desna već sortirana")

    formula = (f"faza spajanja: b_r + b_s = {lchild.out_blocks} + "
               f"{rchild.out_blocks} = {cost}")
    return _join_node("objedinjeno spajanje (sort-merge)", out, cost,
                      [lchild, rchild], cond, formula, extra=napomene)


def join_hash(left: PlanNode, right: PlanNode, cond, M: int) -> PlanNode:
    """Hes spajanje: obe relacije se hesuju u particije po kljucu spajanja, pa
    se spajaju odgovarajuce particije. Trazi ekvi-uslov."""
    out, _ = S.estimate_join(left.stats, right.stats, cond)
    cost = C.hash_join_cost(left.out_blocks, right.out_blocks, M)
    rekurzivno = min(left.out_blocks, right.out_blocks) > M * (M - 1)
    if rekurzivno:
        formula = f"rekurzivno particionisanje -> {cost}"
    else:
        formula = (f"3*(b_r+b_s) = 3*({left.out_blocks}+{right.out_blocks}) = {cost}")
    return _join_node("heš spajanje", out, cost, [left, right], cond, formula)


def index_lookup_cost(table: Table, attr_name: str) -> Optional[Tuple[int, str]]:
    """Cena JEDNE pretrage `attr = ?` najboljim indeksom nad tabelom (ili None).
    Ovo je cena po torki kod indeks ugnezdjene petlje - zato je izdvojena."""
    attr = table.attribute(attr_name)
    SC = max(1, math.ceil(table.row_count / max(attr.distinct_values, 1)))
    mb = max(1, math.ceil(SC / table.rows_per_block))

    najbolji: Optional[Tuple[int, str]] = None
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

        if najbolji is None or cost < najbolji[0]:
            najbolji = (cost, desc)
    return najbolji


def _join_node(algorithm, out_stats, cost, children, cond, formula,
               extra=None) -> PlanNode:
    """Zajednicka fabrika za sve cvorove spajanja (ista struktura detalja)."""
    details = list(extra or [])
    details.append(formula)
    details.append(f"rezultat redova ~= {out_stats.rows}, "
                   f"izlaznih blokova={out_stats.blocks}")
    oznaka = str(cond) if cond is not None else "(Dekartov proizvod)"
    return PlanNode(
        op="Join",
        algorithm=algorithm,
        stats=out_stats,
        processing_cost=cost,
        children=children,
        label=oznaka,
        details=details,
    )


def project(child: PlanNode, kept_keys: List[str], M: int) -> PlanNode:
    """Projekcija bez eliminacije duplikata: jedan linearni prolaz, izbaci
    nepotrebne kolone. Torke postaju uze pa u blok staje vise njih."""
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
    """Eliminacija duplikata (DISTINCT). Bira jeftiniji od dva pristupa:
    sortiranjem (pa izbaci susedne) ili hesiranjem."""
    out = S.apply_distinct(child.stats)
    b = child.out_blocks
    sort_cost = C.external_sort_cost(b, M)
    hash_cost = b if b <= M else 3 * b

    if sort_cost <= hash_cost:
        algo = "eliminacija duplikata sortiranjem"
        cost = sort_cost
        detalj = f"spoljno sortiranje pa izbaci susedne duplikate: {sort_cost}"
    else:
        algo = "eliminacija duplikata heširanjem"
        cost = hash_cost
        detalj = f"heš particije + eliminacija: {hash_cost}"

    return PlanNode(
        op="Distinct", algorithm=algo, stats=out, processing_cost=cost,
        children=[child],
        details=[detalj, f"redova {child.out_rows} -> {out.rows}"],
    )


def aggregate(child: PlanNode, group_keys: List[str], out_attrs: int,
              M: int, distinct_agg: bool = False,
              ulaz_uredjen: bool = False) -> PlanNode:
    """Agregacija / GROUP BY. Bira jeftiniji pristup (hesiranje ili sortiranje).
    `distinct_agg` -> COUNT(DISTINCT ...) koji prvo mora da dedupira.
    `ulaz_uredjen` -> koristi index-only-slicna modifikacija (grupisanje bez
    sortiranja ako je ulaz vec sortiran po kljucu grupisanja)."""
    out = S.apply_aggregation(child.stats, group_keys, out_attrs)
    b = child.out_blocks
    sort_cost = C.external_sort_cost(b, M)
    hash_cost = b if out.blocks <= M else 3 * b

    if GRUPISANJE_BEZ_SORT_AKO_UREDJEN and ulaz_uredjen and group_keys:
        algo = "agregacija nad već sortiranim ulazom (jedan prolaz)"
        cost = b
        detalj = f"ulaz već sortiran po ključu grupisanja -> jedan prolaz: {b}"
    elif hash_cost <= sort_cost:
        algo = "agregacija zasnovana na heširanju"
        cost = hash_cost
        detalj = f"izgradi heš tabelu po ključu grupisanja, jedan prolaz: {hash_cost}"
    else:
        algo = "agregacija zasnovana na sortiranju"
        cost = sort_cost
        detalj = f"spoljno sortiranje po ključu grupisanja pa agregacija: {sort_cost}"

    if group_keys:
        label = "grupisanje po " + ", ".join(group_keys)
    else:
        label = "skalarna agregacija"

    if distinct_agg:
        dedup = C.external_sort_cost(b, M)
        cost += dedup
        detalj += f" + DISTINCT eliminacija: {dedup}"

    return PlanNode(
        op="Aggregation", algorithm=algo, stats=out, processing_cost=cost,
        children=[child], label=label, details=[detalj, f"grupa ~= {out.rows}"],
    )


def set_operation(left: PlanNode, right: PlanNode, op: str, keep_all: bool,
                  M: int) -> PlanNode:
    """Skupovna operacija (UNION / INTERSECT / EXCEPT): sortiraj obe strane pa
    ih spoji jednim prolazom (kao merge)."""
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


def _cheapest_per_algorithm(candidates: List[PlanNode]) -> List[PlanNode]:
    """Isti algoritam moze nastati vise puta (npr. iz razlicitih vodecih uslova)
    - zadrzi samo najjeftiniju varijantu svakog algoritma."""
    najbolji: dict = {}
    for c in candidates:
        postojeci = najbolji.get(c.algorithm)
        if postojeci is None or c.processing_cost < postojeci.processing_cost:
            najbolji[c.algorithm] = c
    return list(najbolji.values())


def _conjuncts_as_comparisons(cond: A.Condition) -> List[A.Comparison]:
    """Poredjenja dostupna na vrhu uslova (samo direktna ili kroz AND)."""
    if isinstance(cond, A.Comparison):
        return [cond]
    if isinstance(cond, A.And):
        return [t for t in cond.terms if isinstance(t, A.Comparison)]
    return []


def _table_column(cmp: A.Comparison, table: Table) -> Optional[A.Column]:
    """Vrati onaj operand poredjenja koji je kolona OVE tabele (ili None)."""
    for operand in (cmp.left, cmp.right):
        if isinstance(operand, A.Column) and table.has_attribute(operand.name):
            return operand
    return None


def _is_single_key_equality(cond: A.Condition, table: Table) -> bool:
    """Da li je ceo uslov jedno `kljuc = vrednost` po JEDINSTVENOM atributu?"""
    if not isinstance(cond, A.Comparison) or cond.op != "=":
        return False
    col = _table_column(cond, table)
    return col is not None and table.attribute(col.name).unique


def objasni_cvor(node: PlanNode, is_root: bool = False) -> str:
    """Citljiv opis jednog cvora: algoritam, cena obrade, upis, formula i
    isprobane alternative."""
    linije = [f"[{node.op}] {node.algorithm}"]
    linije.append(f"  obrada = {node.processing_cost} blok-transfera")
    upis = node.write_cost(is_root)
    if upis:
        linije.append(f"  materijalizacija (upis izlaza) = {upis}")
    linije.append(f"  izlaz ~ {node.out_rows} redova, {node.out_blocks} blokova")
    for d in node.details:
        linije.append(f"  - {d}")
    if len(node.alternatives) > 1:
        linije.append("  isprobane alternative (bira se najjeftinija):")
        for a in node.alternatives:
            linije.append(f"      {a}")
    return "\n".join(linije)


def nabroji_cvorove(plan: PlanNode) -> List[PlanNode]:
    """Vrati sve cvorove plana (koren pa deca), zgodno za obilazak/ispis."""
    redom: List[PlanNode] = []

    def obidji(n: PlanNode) -> None:
        redom.append(n)
        for d in n.children:
            obidji(d)

    obidji(plan)
    return redom
