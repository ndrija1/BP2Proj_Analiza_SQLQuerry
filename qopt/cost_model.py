from __future__ import annotations

import math

HASH_BUCKET_COST = 1


PARALELNI_RADNICI = 1

REPLACEMENT_SELECTION = False


def sort_initial_runs(b: int, M: int) -> int:
    if REPLACEMENT_SELECTION:
        return math.ceil(b / (2 * M))
    return math.ceil(b / M)

def sort_merge_passes(b: int, M: int) -> int:
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


def linear_scan_cost(b_r: int) -> int:
    return math.ceil(b_r / max(1, PARALELNI_RADNICI))

def linear_scan_key_equality_cost(b_r: int) -> int:
    return max(1, math.ceil(b_r / 2))

def binary_search_cost(b_r: int, matching_blocks: int) -> int:
    return math.ceil(math.log2(max(b_r, 1))) + max(0, matching_blocks - 1)

def btree_equality_key_cost(height: int) -> int:
    return height + 1

def btree_clustered_equality_cost(height: int, matching_blocks: int) -> int:
    return height + matching_blocks

def btree_secondary_equality_cost(height: int, matching_records: int) -> int:
    return height + matching_records

def btree_clustered_range_cost(height: int, matching_blocks: int) -> int:
    return height + matching_blocks

def btree_secondary_range_cost(height: int, matching_records: int) -> int:
    return height + matching_records

def hash_equality_key_cost() -> int:
    return HASH_BUCKET_COST + 1

def hash_equality_nonkey_cost(matching_records: int) -> int:
    return HASH_BUCKET_COST + matching_records


def nested_loop_join_cost(b_r: int, n_r: int, b_s: int) -> int:
    return b_r + n_r * b_s

def block_nested_loop_join_cost(b_r: int, b_s: int, M: int) -> int:
    chunks = math.ceil(b_r / max(1, M - 2))
    return b_r + chunks * b_s

def indexed_nested_loop_join_cost(b_r: int, n_r: int, probe_cost: int) -> int:
    return b_r + n_r * probe_cost

def merge_join_cost(b_r: int, b_s: int) -> int:
    return b_r + b_s

def hash_join_cost(b_r: int, b_s: int, M: int) -> int:
    """3*(b_r+b_s) dok build strana staje u M*(M-1) blokova,
    inace rekurzivno particionisanje."""
    b_build = min(b_r, b_s)
    if b_build <= M * (M - 1):
        return 3 * (b_r + b_s)
    passes = math.ceil(math.log(b_build) / math.log(M - 1) - 1)
    return 2 * (b_r + b_s) * max(1, passes) + (b_r + b_s)
