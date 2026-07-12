# Testovi bez zavisnosti - pokretanje: python tests/test_cost_estimator.py
# (radi i kroz pytest). Brojevi u prvom delu su rucno provereni po knjizi.

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qopt import cost_model as C
from qopt import statistics as S
from qopt.catalog import Catalog
from qopt.optimizer import optimize
from qopt.parser import parse
from qopt.physical import plan_cost
from qopt.semantics import analyze

SCHEMA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "test.json")

_passed = 0


def check(name, got, want):
    global _passed
    assert got == want, f"{name}: dobijeno {got}, očekivano {want}"
    _passed += 1


def approx(name, got, want, tol=1e-6):
    global _passed
    assert abs(got - want) <= tol, f"{name}: dobijeno {got}, očekivano ~{want}"
    _passed += 1


# ---- formule cene ---------------------------------------------------------

def test_cost_formulas():
    # spoljno sortiranje: b=800, M=10 -> 80 tura, 2 prolaza -> 800*(2*2+1)
    check("sort_runs", C.sort_initial_runs(800, 10), 80)
    check("sort_passes", C.sort_merge_passes(800, 10), 2)
    check("external_sort", C.external_sort_cost(800, 10), 4000)
    # staje u bafer -> samo citanje
    check("sort_fits", C.external_sort_cost(8, 10), 8)

    check("btree_key", C.btree_equality_key_cost(3), 4)
    check("hash_key", C.hash_equality_key_cost(), 2)
    check("hash_nonkey", C.hash_equality_nonkey_cost(2), 3)
    check("btree_secondary", C.btree_secondary_equality_cost(3, 8), 11)

    check("nlj", C.nested_loop_join_cost(100, 1000, 800), 800100)
    check("bnlj_inner_fits", C.block_nested_loop_join_cost(8, 800, 10), 808)
    # 100 blokova u komadima od M-2=8 -> ceil(100/8)=13 prolaza po s
    check("bnlj_chunks", C.block_nested_loop_join_cost(100, 800, 10), 10500)
    check("hash_join", C.hash_join_cost(800, 30, 10), 2490)
    check("merge_join", C.merge_join_cost(134, 100), 234)
    check("inlj", C.indexed_nested_loop_join_cost(40, 200, 11), 2240)


# ---- statistika / selektivnost --------------------------------------------

def test_statistics():
    cat = Catalog.from_json_file(SCHEMA)
    student = S.base_table_stats(cat.table("Student"), "Student")

    # jednakost po ne-kljucu: 1/V
    q = parse("SELECT * FROM Student WHERE ime = 'Marko'")
    analyze(q, cat)
    _, sel = S.apply_selection(student, q.where)
    approx("eq_nonkey_selectivity", sel, 1.0 / 800)

    # opseg: 1/3
    q = parse("SELECT * FROM Student WHERE prosek > 8.0")
    analyze(q, cat)
    _, sel = S.apply_selection(student, q.where)
    approx("range_selectivity", sel, 1.0 / 3.0)

    # jednakost po kljucu -> jedan red
    q = parse("SELECT * FROM Student WHERE indeks = '2020/0001'")
    analyze(q, cat)
    out, _ = S.apply_selection(student, q.where)
    check("key_eq_one_row", out.rows, 1)


# ---- ceo lanac: izbor algoritma i ukupna cena ------------------------------

def _plan(sql):
    cat = Catalog.from_json_file(SCHEMA)
    q = parse(sql)
    analyze(q, cat)
    return optimize(q, cat), cat


def _algorithms(node, acc=None):
    acc = acc if acc is not None else []
    acc.append(node.algorithm)
    for ch in node.children:
        _algorithms(ch, acc)
    return acc


def test_end_to_end():
    # pretraga po kljucu sortirajuceg B+ stabla: HT + 1 = 4
    plan, _ = _plan("SELECT * FROM Student WHERE indeks = '2020/0001'")
    check("key_lookup_cost", plan_cost(plan), 4)
    assert any("B+-stablo" in a and "ključ" in a for a in _algorithms(plan))

    # jednakost po ne-kljucu preko hes indeksa
    plan, _ = _plan("SELECT ime FROM Student WHERE ime = 'Marko'")
    assert any("heš indeks" in a for a in _algorithms(plan)), _algorithms(plan)

    # mala nesortirana + malena sortirana -> blok petlja
    plan, _ = _plan("SELECT * FROM Ispit i, Predmet p WHERE i.predmetId = p.predmetId")
    assert "spajanje blok ugnežđenom petljom" in _algorithms(plan), _algorithms(plan)

    # slozeno ekvi-spajanje bez korisnog sortiranja -> hes
    plan, _ = _plan("SELECT * FROM Stipendija st, Ispit i "
                    "WHERE st.studentIndeks = i.studentIndeks AND st.iznos = i.ocena")
    assert "heš spajanje" in _algorithms(plan), _algorithms(plan)

    # disjunktivni uslov spajanja ne sme na hes/merge
    plan, _ = _plan("SELECT * FROM Student s, Stipendija st "
                    "WHERE s.indeks = st.studentIndeks OR s.smer = st.tip")
    algs = _algorithms(plan)
    assert ("spajanje blok ugnežđenom petljom" in algs
            or "spajanje ugnežđenom petljom" in algs), algs
    assert "heš spajanje" not in algs, algs
    assert "objedinjeno spajanje (sort-merge)" not in algs, algs


# ---- ogranicenja gramatike iz teksta projekta ------------------------------

def test_grammar_limits():
    _expect_reject("SELECT s.ime FROM Student s, Ispit i, Predmet p, Stipendija st, "
                   "Student s2 WHERE s.indeks = i.studentIndeks", "ograničenje je 4")
    _expect_reject("SELECT s.ime FROM Student s INNER JOIN Ispit i "
                   "ON s.indeks = i.studentIndeks", "JOIN operator")
    _expect_reject("SELECT ime FROM Student WHERE prosek>1 AND prosek>2 AND prosek>3 "
                   "AND prosek>4 AND prosek>5 AND prosek>6 AND prosek>7",
                   "ograničenje je 6")
    _expect_reject("SELECT ime FROM Student ORDER BY prosek, ime", "ograničenje je 1")


def _expect_reject(sql, needle):
    global _passed
    try:
        _plan(sql)
        raise AssertionError(f"trebalo je odbiti: {sql}")
    except AssertionError:
        raise
    except Exception as e:
        assert needle in str(e), f"očekivana poruka sa '{needle}', dobijeno: {e}"
        _passed += 1


# ---- logika plana i robusnost ulaza ----------------------------------------

def test_plan_logic_and_input():
    global _passed

    # ORDER BY po atributu koji SELECT ne zadrzava -> Sort ISPOD projekcije
    plan, _ = _plan("SELECT ime FROM Student WHERE prosek > 8.0 ORDER BY prosek")
    ops = [n.op for n in _walk(plan)]
    check("sort_below_projection",
          ops.index("Projection") < ops.index("Sort"), True)

    # ORDER BY po zadrzanom atributu -> Sort na vrhu (uze torke)
    plan, _ = _plan("SELECT ime, prosek FROM Student ORDER BY prosek")
    ops = [n.op for n in _walk(plan)]
    check("sort_on_top_when_kept", ops.index("Sort") < ops.index("Projection"), True)

    # ORDER BY uz agregaciju mora po atributu grupisanja
    _expect_reject("SELECT smer, COUNT(*) FROM Student GROUP BY smer ORDER BY prosek",
                   "GROUP BY")

    # los JSON daje CatalogError, ne KeyError
    import json
    from qopt.errors import CatalogError
    d = json.load(open(SCHEMA, encoding="utf-8"))
    d["schema"]["tables"][0].pop("rowCount")
    try:
        Catalog.from_dict(d)
        raise AssertionError("nedostajuće rowCount je prihvaćeno")
    except CatalogError as e:
        assert "rowCount" in str(e), str(e)
        _passed += 1


def _walk(node, acc=None):
    acc = [] if acc is None else acc
    acc.append(node)
    for ch in node.children:
        _walk(ch, acc)
    return acc


def run_all():
    for fn in (test_cost_formulas, test_statistics, test_end_to_end,
               test_grammar_limits, test_plan_logic_and_input):
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\nSVI TESTOVI PROŠLI ({_passed} provera)")


if __name__ == "__main__":
    run_all()
