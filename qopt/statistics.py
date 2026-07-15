from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from . import ast_nodes as A
from .catalog import Table


PRETPOSTAVI_NEZAVISNOST = True

HISTOGRAM_OPSEZI: Dict[str, Tuple[float, float]] = {}

FK_STRANI_KLJUCEVI: set = set()


def col_key(col: A.Column) -> str:
    """Kljuc kolone u recniku razlicitih vrednosti: "binding.naziv".
    Npr. za kolonu `s.prosek` -> "s.prosek"."""
    return f"{col.table}.{col.name}"


def blocks_for(n_rows: float, f: float) -> int:
    """Koliko blokova zauzme n_rows redova ako u blok staje f redova.
    Uvek bar 1 blok (prazan rezultat i dalje "postoji"). max(f, ~0) cuva od
    deljenja nulom."""
    f_bezbedno = max(f, 1e-9)
    return max(1, math.ceil(n_rows / f_bezbedno))


def _clamp01(x: float) -> float:
    """Ogranici verovatnocu/selektivnost na opseg [0, 1]."""
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


@dataclass
class RelationStats:
    """Procenjena velicina tabele ili medjurezultata; putuje uz plan navise.

    Polja:
      n_rows   - procenjen broj redova (float, jer je procena)
      f        - redova po bloku (blocking factor)
      n_attrs  - broj kolona (bitno za projekciju: uze torke -> vece f)
      distinct - mapa "binding.kolona" -> V(A) (broj razlicitih vrednosti)
    """

    n_rows: float
    f: float
    n_attrs: int
    distinct: Dict[str, float] = field(default_factory=dict)

    @property
    def blocks(self) -> int:
        """Broj blokova na disku za ovoliko redova."""
        return blocks_for(self.n_rows, self.f)

    @property
    def rows(self) -> int:
        """Broj redova zaokruzen na ceo broj (za prikaz), bar 1."""
        return max(1, round(self.n_rows))

    def V(self, key: str) -> float:
        """V(A) za kolonu. Ako je ne znamo, najgori (najkonzervativniji) slucaj
        je da je svaka vrednost razlicita -> V = broj redova."""
        return self.distinct.get(key, self.n_rows)

    def copy(self) -> "RelationStats":
        """Duboka kopija (recnik se kopira) da izmene ne cure izmedju planova."""
        return RelationStats(self.n_rows, self.f, self.n_attrs, dict(self.distinct))


def base_table_stats(table: Table, binding: str) -> RelationStats:
    """Pocetna statistika bazne tabele, direktno iz kataloga (knjiga: n_r, b_r,
    f_r, V(A))."""
    distinct: Dict[str, float] = {}
    for a in table.attributes:
        distinct[f"{binding}.{a.name}"] = float(a.distinct_values)

    return RelationStats(
        n_rows=float(table.row_count),
        f=float(table.rows_per_block),
        n_attrs=len(table.attributes),
        distinct=distinct,
    )


def selectivity(cond: Optional[A.Condition], vlookup: Callable[[str], float]) -> float:
    """Deo redova (0..1) koji zadovoljava uslov.

    `vlookup(key)` vraca V(A) za kolonu - prosledjuje ga pozivalac (za selekciju
    to je stats.V, za spajanje spojena mapa obe strane).

    Idemo rekurzivno kroz stablo uslova: AND/OR/NOT kombinuju decu, a list
    (poredjenje) je osnovni slucaj.
    """
    if cond is None:
        return 1.0

    if isinstance(cond, A.And):
        return _selectivity_and(cond, vlookup)

    if isinstance(cond, A.Or):
        return _selectivity_or(cond, vlookup)

    if isinstance(cond, A.Not):
        return _clamp01(1.0 - selectivity(cond.term, vlookup))

    if isinstance(cond, A.Comparison):
        return _comparison_selectivity(cond, vlookup)

    return 1.0


def _selectivity_and(cond: A.And, vlookup) -> float:
    """AND: kombinuje selektivnosti svih clanova."""
    pojedinacne = [selectivity(t, vlookup) for t in cond.terms]

    if PRETPOSTAVI_NEZAVISNOST:
        s = 1.0
        for si in pojedinacne:
            s *= si
        return _clamp01(s)

    return _clamp01(min(pojedinacne)) if pojedinacne else 1.0


def _selectivity_or(cond: A.Or, vlookup) -> float:
    """OR: verovatnoca da bar jedan clan pogodi = 1 - (nijedan ne pogodi).
    "nijedan ne pogodi" = proizvod komplemenata (1 - si)."""
    prod_komplemenata = 1.0
    for t in cond.terms:
        si = selectivity(t, vlookup)
        prod_komplemenata *= (1.0 - si)
    return _clamp01(1.0 - prod_komplemenata)


def _comparison_selectivity(cmp: A.Comparison, vlookup) -> float:
    """Selektivnost jednog poredjenja (list stabla uslova)."""
    left, right = cmp.left, cmp.right
    is_eq = cmp.op == "="
    is_neq = cmp.op in ("<>", "!=")
    left_col = isinstance(left, A.Column)
    right_col = isinstance(right, A.Column)

    if cmp.op == "LIKE":
        return 0.1

    if left_col and right_col:
        return _selectivity_col_col(left, right, is_eq, is_neq, vlookup)

    if left_col or right_col:
        col = left if left_col else right
        return _selectivity_col_const(cmp, col, is_eq, is_neq, vlookup)

    return _const_compare(cmp)


def _selectivity_col_col(left, right, is_eq, is_neq, vlookup) -> float:
    """kolona op kolona: koristimo veci V od dve kolone (konzervativno)."""
    v = max(vlookup(col_key(left)), vlookup(col_key(right)), 1.0)
    if is_eq:
        return 1.0 / v
    if is_neq:
        return _clamp01(1.0 - 1.0 / v)
    return 1.0 / 3.0


def _selectivity_col_const(cmp, col, is_eq, is_neq, vlookup) -> float:
    """kolona op konstanta."""
    v = max(vlookup(col_key(col)), 1.0)
    if is_eq:
        return 1.0 / v
    if is_neq:
        return _clamp01(1.0 - 1.0 / v)

    hist = _histogram_selektivnost(cmp, col)
    if hist is not None:
        return hist

    return 1.0 / 3.0


def _histogram_selektivnost(cmp: A.Comparison, col: A.Column) -> Optional[float]:
    """Selektivnost opsega preko min/max granica kolone (linearni "histogram").
    Vraca None ako granice nisu poznate -> pozivalac padne na 1/3."""
    granice = HISTOGRAM_OPSEZI.get(col_key(col))
    if granice is None:
        return None

    lo, hi = granice
    raspon = hi - lo
    if raspon <= 0:
        return 1.0 / 3.0

    druga = cmp.right if cmp.left is col else cmp.left
    if not isinstance(druga, A.Literal) or not isinstance(druga.value, (int, float)):
        return None
    v = float(druga.value)

    op = cmp.op
    if op in (">", ">="):
        deo = (hi - v) / raspon
    elif op in ("<", "<="):
        deo = (v - lo) / raspon
    else:
        return None
    return _clamp01(deo)


def _const_compare(cmp: A.Comparison) -> float:
    """Obe strane su konstante (npr. 1 = 1): rezultat je ili sve (1.0) ili
    nista (0.0), pa direktno izracunamo istinitost."""
    try:
        lv = cmp.left.value
        rv = cmp.right.value
        operacije = {
            "=":  lambda a, b: a == b,
            "<>": lambda a, b: a != b,
            "!=": lambda a, b: a != b,
            "<":  lambda a, b: a < b,
            "<=": lambda a, b: a <= b,
            ">":  lambda a, b: a > b,
            ">=": lambda a, b: a >= b,
        }
        istina = operacije[cmp.op](lv, rv)
        return 1.0 if istina else 0.0
    except Exception:
        return 1.0


def apply_selection(stats: RelationStats, cond: A.Condition) -> Tuple[RelationStats, float]:
    """Statistika posle selekcije + izracunata selektivnost.
    Vraca (nova_statistika, selektivnost)."""
    sel = selectivity(cond, stats.V)
    n_new = max(1.0, stats.n_rows * sel)

    new = stats.copy()
    new.n_rows = n_new

    for k in list(new.distinct):
        new.distinct[k] = min(new.distinct[k], n_new)

    for cmp in _top_level_equalities(cond):
        for operand in (cmp.left, cmp.right):
            if isinstance(operand, A.Column):
                new.distinct[col_key(operand)] = 1.0

    return new, sel


def _top_level_equalities(cond: Optional[A.Condition]) -> List[A.Comparison]:
    """Jednakosti (A = ...) dostupne preko lanca AND-ova na vrhu.
    Kroz OR NE idemo - tamo jednakost nije garantovana za svaki red."""
    if cond is None:
        return []
    if isinstance(cond, A.Comparison):
        return [cond] if cond.op == "=" else []
    if isinstance(cond, A.And):
        out: List[A.Comparison] = []
        for t in cond.terms:
            out.extend(_top_level_equalities(t))
        return out
    return []


def estimate_join(left: RelationStats, right: RelationStats,
                  cond: Optional[A.Condition]) -> Tuple[RelationStats, float]:
    """Statistika rezultata spajanja leve i desne relacije.
    cond=None znaci Dekartov proizvod (svaki sa svakim)."""
    merged: Dict[str, float] = {}
    merged.update(left.distinct)
    merged.update(right.distinct)

    podrazumevano_v = max(left.n_rows, right.n_rows)
    sel = selectivity(cond, lambda k: merged.get(k, podrazumevano_v))

    cross = left.n_rows * right.n_rows
    n_new = max(1.0, min(cross, cross * sel))

    n_fk = _fk_procena(cond, left, right)
    if n_fk is not None:
        n_new = max(1.0, min(cross, n_fk))

    f_join = (left.f * right.f) / (left.f + right.f)

    new = RelationStats(
        n_rows=n_new,
        f=f_join,
        n_attrs=left.n_attrs + right.n_attrs,
        distinct=dict(merged),
    )

    if cond is not None:
        for cmp in _top_level_equalities(cond):
            if isinstance(cmp.left, A.Column) and isinstance(cmp.right, A.Column):
                lk, rk = col_key(cmp.left), col_key(cmp.right)
                zajednicko = min(merged.get(lk, n_new), merged.get(rk, n_new))
                new.distinct[lk] = zajednicko
                new.distinct[rk] = zajednicko

    for k in list(new.distinct):
        new.distinct[k] = min(new.distinct[k], n_new)

    return new, sel


def _fk_procena(cond, left: RelationStats, right: RelationStats) -> Optional[float]:
    """Ako je neka ekvi-jednakost preko kolone iz FK_STRANI_KLJUCEVI, vrati broj
    redova strane kojoj ta kolona pripada (dete). Inace None."""
    if not FK_STRANI_KLJUCEVI or cond is None:
        return None
    for cmp in _top_level_equalities(cond):
        if not (isinstance(cmp.left, A.Column) and isinstance(cmp.right, A.Column)):
            continue
        lk, rk = col_key(cmp.left), col_key(cmp.right)
        if lk in FK_STRANI_KLJUCEVI:
            return left.n_rows
        if rk in FK_STRANI_KLJUCEVI:
            return right.n_rows
    return None


def apply_projection(stats: RelationStats, kept_keys: List[str],
                     distinct_elim: bool) -> RelationStats:
    """Projekcija: zadrzavamo samo neke kolone -> uze torke -> veci faktor
    blokiranja (vise redova u blok). Uz eliminaciju duplikata broj redova pada
    na broj razlicitih kombinacija zadrzanih kolona."""
    kept = max(1, len(kept_keys))

    f_new = stats.f * (stats.n_attrs / kept)

    n_new = stats.n_rows
    if distinct_elim:
        combos = 1.0
        for k in kept_keys:
            combos *= stats.V(k)
        n_new = min(stats.n_rows, combos)

    nova_distinct = {k: min(stats.V(k), n_new) for k in kept_keys}
    return RelationStats(
        n_rows=max(1.0, n_new),
        f=f_new,
        n_attrs=kept,
        distinct=nova_distinct,
    )


def apply_aggregation(stats: RelationStats, group_keys: List[str],
                      n_out_attrs: int) -> RelationStats:
    """Grupisanje: jedan izlazni red po grupi. Bez GROUP BY -> jedna grupa
    (skalarna agregacija, npr. COUNT(*) cele tabele)."""
    if not group_keys:
        n_groups = 1.0
    else:
        n_groups = 1.0
        for k in group_keys:
            n_groups *= stats.V(k)
        n_groups = min(stats.n_rows, n_groups)

    f_new = stats.f * (stats.n_attrs / max(1, n_out_attrs))
    nova_distinct = {k: min(stats.V(k), n_groups) for k in group_keys}
    return RelationStats(
        n_rows=max(1.0, n_groups),
        f=f_new,
        n_attrs=n_out_attrs,
        distinct=nova_distinct,
    )


def apply_distinct(stats: RelationStats) -> RelationStats:
    """DISTINCT nad celim redom: broj redova pada na broj razlicitih kombinacija
    svih kolona (procena = proizvod V svih kolona, ogranicen brojem redova)."""
    if stats.distinct:
        combos = 1.0
        for k in stats.distinct:
            combos *= stats.distinct[k]
        n_new = min(stats.n_rows, combos)
    else:
        n_new = stats.n_rows

    new = stats.copy()
    new.n_rows = max(1.0, n_new)
    return new


def set_operation_stats(op: str, left: RelationStats, right: RelationStats,
                        keep_all: bool) -> RelationStats:
    """Procena velicine skupovne operacije. Bez podataka o preklapanju vrednosti
    koristimo grube granice."""
    if op == "UNION":
        n = left.n_rows + right.n_rows
    elif op == "INTERSECT":
        n = min(left.n_rows, right.n_rows) / 2.0
    else:
        n = left.n_rows / 2.0

    return RelationStats(
        n_rows=max(1.0, n),
        f=(left.f + right.f) / 2.0,
        n_attrs=left.n_attrs,
        distinct={},
    )


def objasni_selektivnost(cond: Optional[A.Condition],
                         vlookup: Callable[[str], float]) -> str:
    """Korak-po-korak izvod selektivnosti datog uslova."""
    linije: List[str] = []

    def opisi(c: Optional[A.Condition], dubina: int) -> float:
        uvlaka = "  " * dubina
        if c is None:
            linije.append(f"{uvlaka}(nema uslova) -> 1.0")
            return 1.0
        if isinstance(c, A.And):
            deca = [opisi(t, dubina + 1) for t in c.terms]
            if PRETPOSTAVI_NEZAVISNOST:
                s = 1.0
                for d in deca:
                    s *= d
                linije.append(f"{uvlaka}AND (nezavisnost) = proizvod = {s:.5f}")
            else:
                s = min(deca) if deca else 1.0
                linije.append(f"{uvlaka}AND (korelacija) = min = {s:.5f}")
            return _clamp01(s)
        if isinstance(c, A.Or):
            deca = [opisi(t, dubina + 1) for t in c.terms]
            prod = 1.0
            for d in deca:
                prod *= (1.0 - d)
            s = _clamp01(1.0 - prod)
            linije.append(f"{uvlaka}OR = 1 - proizvod(1-si) = {s:.5f}")
            return s
        if isinstance(c, A.Not):
            d = opisi(c.term, dubina + 1)
            s = _clamp01(1.0 - d)
            linije.append(f"{uvlaka}NOT = 1 - {d:.5f} = {s:.5f}")
            return s
        s = _comparison_selectivity(c, vlookup)
        linije.append(f"{uvlaka}{c}  ->  selektivnost = {s:.5f}")
        return s

    ukupno = opisi(cond, 0)
    linije.append(f"UKUPNA SELEKTIVNOST = {ukupno:.5f}")
    return "\n".join(linije)


def objasni_relaciju(stats: RelationStats, naziv: str = "relacija") -> str:
    """Kratak pregled statistike jedne relacije (redovi, blokovi, V po koloni)."""
    linije = [f"{naziv}: n_rows~{stats.rows}, f~{stats.f:.2f}, "
              f"blokova={stats.blocks}, kolona={stats.n_attrs}"]
    for k in sorted(stats.distinct):
        linije.append(f"    V({k}) = {stats.distinct[k]:.0f}")
    return "\n".join(linije)
