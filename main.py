# Ulazna tacka. Sema + velicina bafera se citaju iz JSON-a (default test.json),
# upiti iz --query, --query-file ili sa stdin-a (vise upita razdvojiti sa ';').
#
# Primeri:
#   python main.py --query "SELECT * FROM Student WHERE prosek > 9"
#   python main.py --query-file queries/all_examples.sql
#   python main.py --schema test.json --query "..." --debug

from __future__ import annotations

import argparse
import sys

# forsiraj UTF-8 da ispis (c, s, z...) ne pukne na cp1252 konzoli / redirekciji
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from qopt.catalog import Catalog
from qopt.errors import QoptError
from qopt.explain import render
from qopt.optimizer import optimize
from qopt.parser import parse
from qopt.semantics import analyze


def main(argv=None) -> int:
    args = _parse_args(argv)

    try:
        catalog = Catalog.from_json_file(args.schema)
    except (OSError, QoptError) as e:
        print(f"greška: ne mogu da učitam šemu '{args.schema}': {e}", file=sys.stderr)
        return 2

    query_text = _read_query_source(args)
    if not query_text.strip():
        print("greška: nije zadat upit (koristite --query, --query-file ili stdin)",
              file=sys.stderr)
        return 2

    failures = 0
    for stmt in _split_statements(query_text):
        if not run_one(stmt, catalog, show_alt=not args.no_alternatives,
                       debug=args.debug):
            failures += 1
    return 1 if failures else 0


def run_one(query_text: str, catalog: Catalog, show_alt: bool, debug: bool) -> bool:
    """Jedan upit kroz ceo lanac; na gresku ispisi poruku i nastavi dalje."""
    try:
        ast = parse(query_text)                     # sintaksa
        if debug:
            print("AST:", ast)
        analyze(ast, catalog)                       # semantika
        plan = optimize(ast, catalog)               # optimizacija
        print(render(plan, catalog, query_text, show_alternatives=show_alt))
        print()
        return True
    except QoptError as e:
        print("-" * 72)
        print(f"UPIT: {query_text.strip()}")
        print(f"ODBIJENO ({type(e).__name__}): {e}")
        print("-" * 72)
        print()
        return False


def _parse_args(argv):
    p = argparse.ArgumentParser(
        description="Procena cene i najboljeg plana izvršavanja SQL upita.")
    p.add_argument("--schema", default="test.json",
                   help="JSON datoteka sa veličinom bafera + šemom (podrazumevano: test.json)")
    p.add_argument("--query", help="tekst SQL upita")
    p.add_argument("--query-file", help="datoteka sa jednim ili više upita")
    p.add_argument("--no-alternatives", action="store_true",
                   help="sakrij listu odbačenih algoritama")
    p.add_argument("--debug", action="store_true",
                   help="ispiši i parsirano AST stablo svakog upita")
    return p.parse_args(argv)


def _read_query_source(args) -> str:
    if args.query:
        return args.query
    if args.query_file:
        with open(args.query_file, "r", encoding="utf-8") as fh:
            return fh.read()
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def _split_statements(text: str):
    # podeli po ';', preskoci prazno i '--' komentare
    cleaned = "\n".join(line for line in text.splitlines()
                        if not line.strip().startswith("--"))
    for stmt in cleaned.split(";"):
        if stmt.strip():
            yield stmt.strip()


if __name__ == "__main__":
    raise SystemExit(main())
