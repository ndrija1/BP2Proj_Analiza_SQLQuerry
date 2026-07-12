# Ispis plana kao stablo, sa algoritmom, cenom, formulom i odbacenim
# alternativama za svaki cvor. Interni op kljucevi ostaju engleski (po njima
# grana optimizator), ovde se samo prevode za prikaz.

from __future__ import annotations

from typing import List

from .catalog import Catalog
from .physical import PlanNode, plan_cost

OP_NAMES = {
    "Relation": "Relacija",
    "Scan": "Skeniranje",
    "Selection": "Selekcija",
    "Sort": "Sortiranje",
    "Join": "Spajanje",
    "Projection": "Projekcija",
    "Distinct": "Eliminacija duplikata",
    "Aggregation": "Agregacija",
    "SetOp": "Skupovna operacija",
}


def render(plan: PlanNode, catalog: Catalog, query_text: str = "",
           show_alternatives: bool = True) -> str:
    lines: List[str] = []
    total = plan_cost(plan, is_root=True)

    lines.append("=" * 72)
    if query_text:
        lines.append("UPIT:")
        lines.append("  " + query_text.strip())
        lines.append("-" * 72)
    lines.append(f"NAJBOLJI PLAN  (bafer M = {catalog.buffer_blocks} blokova, "
                 f"jedinica cene = blok transfer, model = materijalizacija)")
    lines.append(f"UKUPNA PROCENJENA CENA = {total} blok transfera")
    lines.append("=" * 72)

    _render(plan, "", True, True, lines, show_alternatives)
    lines.append("=" * 72)
    return "\n".join(lines)


def _render(node: PlanNode, prefix: str, is_last: bool, is_root: bool,
            lines: List[str], show_alt: bool) -> None:
    # ASCII crtanje stabla da radi u svakoj konzoli
    connector = "" if is_root else ("\\- " if is_last else "+- ")
    own = node.node_cost(is_root)
    subtree = plan_cost(node, is_root)

    lines.append(f"{prefix}{connector}{OP_NAMES.get(node.op, node.op)}  "
                 f"[{node.algorithm}]")
    child_prefix = prefix + ("" if is_root else ("    " if is_last else "|   "))
    lines.append(f"{child_prefix}cena +{own}  (zbir={subtree})   "
                 f"~{node.out_rows} redova, {node.out_blocks} blk")

    if node.label:
        lines.append(f"{child_prefix}* {node.label}")
    for d in node.details:
        lines.append(f"{child_prefix}  - {d}")

    write = node.write_cost(is_root)
    if write:
        lines.append(f"{child_prefix}  - materijalizuj rezultat: +{write} blk "
                     f"upisano (obrada {node.processing_cost} + upis {write})")

    if show_alt and len(node.alternatives) > 1:
        lines.append(f"{child_prefix}  - isprobani algoritmi (bira se najjeftiniji): "
                     + " | ".join(node.alternatives))

    for i, child in enumerate(node.children):
        _render(child, child_prefix, i == len(node.children) - 1, False,
                lines, show_alt)
