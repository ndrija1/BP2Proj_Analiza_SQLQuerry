# Formule cene u broju blok transfera, po Silberschatz-u (pogl. 12-13).
# Sve su ciste funkcije nad brojevima pa se lako porede sa knjigom i testiraju.
#
# Upis rezultata (materijalizacija) se NE dodaje ovde nego jednom, uniformno,
# u physical.py - zato ove formule odgovaraju knjizi jedan-na-jedan.

from __future__ import annotations

import math

# pretraga hes indeksa prvo cita blok kofe
HASH_BUCKET_COST = 1


# ---- spoljno objedinjeno sortiranje ------------------------------------

def sort_initial_runs(b: int, M: int) -> int:
    # koliko sortiranih tura nastane u prvom prolazu
    return math.ceil(b / M)

def sort_merge_passes(b: int, M: int) -> int:
    # svaki prolaz spaja do M-1 tura u jednu
    runs = sort_initial_runs(b, M)
    if runs <= 1:
        return 0
    return math.ceil(math.log(runs) / math.log(M - 1))

def external_sort_cost(b: int, M: int) -> int:
    """b * (2*prolaza + 1), bez zavrsnog upisa (njega dodaje physical.py).
    Ako sve staje u bafer (b <= M) nema prolaza spajanja, ostaje samo citanje."""
    if b <= 0:
        return 0
    return b * (2 * sort_merge_passes(b, M) + 1)


# ---- pristupni putevi selekcije (algoritmi A1..A6) ----------------------

def linear_scan_cost(b_r: int) -> int:
    return b_r

def linear_scan_key_equality_cost(b_r: int) -> int:
    # jednakost po kljucu: u proseku stanemo posle pola fajla
    return max(1, math.ceil(b_r / 2))

def binary_search_cost(b_r: int, matching_blocks: int) -> int:
    # fajl sortiran po atributu: ceil(log2(b_r)) + (blokova pogodaka - 1)
    return math.ceil(math.log2(max(b_r, 1))) + max(0, matching_blocks - 1)

def btree_equality_key_cost(height: int) -> int:
    # spusti se niz stablo pa procitaj blok sa slogom
    return height + 1

def btree_clustered_equality_cost(height: int, matching_blocks: int) -> int:
    # sortirajuci indeks: pogoci su susedni blokovi
    return height + matching_blocks

def btree_secondary_equality_cost(height: int, matching_records: int) -> int:
    # sekundarni indeks: svaki pogodak moze biti u drugom bloku
    return height + matching_records

def btree_clustered_range_cost(height: int, matching_blocks: int) -> int:
    return height + matching_blocks

def btree_secondary_range_cost(height: int, matching_records: int) -> int:
    # cesto gore od linearnog citanja - zato ih optimizator poredi
    return height + matching_records

def hash_equality_key_cost() -> int:
    return HASH_BUCKET_COST + 1

def hash_equality_nonkey_cost(matching_records: int) -> int:
    return HASH_BUCKET_COST + matching_records


# ---- spajanja -----------------------------------------------------------

def nested_loop_join_cost(b_r: int, n_r: int, b_s: int) -> int:
    # po torki spoljne relacije jednom procitamo celu unutrasnju
    return b_r + n_r * b_s

def block_nested_loop_join_cost(b_r: int, b_s: int, M: int) -> int:
    # spoljna ide u komadima od M-2 bloka; ako cela stane -> b_r + b_s
    chunks = math.ceil(b_r / max(1, M - 2))
    return b_r + chunks * b_s

def indexed_nested_loop_join_cost(b_r: int, n_r: int, probe_cost: int) -> int:
    # za svaku torku spoljne jedna pretraga indeksa nad unutrasnjom
    return b_r + n_r * probe_cost

def merge_join_cost(b_r: int, b_s: int) -> int:
    # oba ulaza VEC sortirana; sortiranje se placa posebnim Sort cvorom
    return b_r + b_s

def hash_join_cost(b_r: int, b_s: int, M: int) -> int:
    """3*(b_r+b_s) dok build strana staje u M*(M-1) blokova,
    inace rekurzivno particionisanje."""
    b_build = min(b_r, b_s)
    if b_build <= M * (M - 1):
        return 3 * (b_r + b_s)
    passes = math.ceil(math.log(b_build) / math.log(M - 1) - 1)
    return 2 * (b_r + b_s) * max(1, passes) + (b_r + b_s)
