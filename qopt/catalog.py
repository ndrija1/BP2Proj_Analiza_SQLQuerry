# Sistemski katalog: sema baze + statistika, ucitava se iz JSON fajla.
#
# Oznake pratim kao u knjizi (Silberschatz):
#   n_r  - broj redova u tabeli        (rowCount)
#   b_r  - broj blokova na disku       (blockCount)
#   f_r  - broj redova po bloku        (rowsPerBlock)
#   V(A) - broj razlicitih vrednosti   (distinctValues)
#   M    - velicina bafera u blokovima (bufferBlocks)

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .errors import CatalogError

NUMERIC_TYPES = {"INT", "DOUBLE"}
ALL_TYPES = {"INT", "DOUBLE", "STRING", "DATE"}

B_PLUS_TREE = "B_PLUS_TREE"
HASH = "HASH"


def is_numeric(type_name: str) -> bool:
    return type_name in NUMERIC_TYPES


def types_comparable(a: str, b: str) -> bool:
    # isti tip uvek moze, INT i DOUBLE mogu medjusobno
    if a == b:
        return True
    return is_numeric(a) and is_numeric(b)


@dataclass
class Attribute:
    name: str
    type: str
    unique: bool = False
    distinct_values: int = 1    # V(A, r)


@dataclass
class Index:
    name: str
    attributes: List[str]       # redosled je bitan (levi prefiks)
    kind: str                   # B_PLUS_TREE ili HASH
    clustered: bool = False     # da li je fajl sortiran po ovim atributima
    tree_height: int = 0        # visina stabla, samo za B+

    @property
    def is_tree(self) -> bool:
        return self.kind == B_PLUS_TREE

    @property
    def is_hash(self) -> bool:
        return self.kind == HASH

    def leading_attribute(self) -> str:
        # prvi atribut - jedini po kome indeks ume da pretrazuje samostalno
        return self.attributes[0]

    def covers_prefix(self, attrs: List[str]) -> bool:
        """Pravilo levog prefiksa: slozeni indeks nad (smer, prosek) pokriva
        pretragu po (smer) ili (smer, prosek), ali ne po samom (prosek)."""
        if len(attrs) > len(self.attributes):
            return False
        return all(a == b for a, b in zip(attrs, self.attributes))


@dataclass
class Table:
    name: str
    row_count: int              # n_r
    block_count: int            # b_r
    rows_per_block: int         # f_r
    attributes: List[Attribute] = field(default_factory=list)
    indexes: List[Index] = field(default_factory=list)

    def attribute(self, name: str) -> Optional[Attribute]:
        for a in self.attributes:
            if a.name == name:
                return a
        return None

    def has_attribute(self, name: str) -> bool:
        return self.attribute(name) is not None

    def distinct_values(self, attr_name: str) -> int:
        """V(A) - broj razlicitih vrednosti atributa."""
        a = self.attribute(attr_name)
        if a is None:
            raise CatalogError(f"{self.name} nema atribut '{attr_name}'")
        return a.distinct_values

    def indexes_on(self, attr_name: str) -> List[Index]:
        """Indeksi ciji je vodeci atribut bas attr_name."""
        return [ix for ix in self.indexes if ix.leading_attribute() == attr_name]

    def clustered_index(self) -> Optional[Index]:
        for ix in self.indexes:
            if ix.clustered:
                return ix
        return None

    def is_sorted_on(self, attr_name: str) -> bool:
        """Da li je fajl fizicki sortiran po ovom atributu."""
        ix = self.clustered_index()
        return ix is not None and ix.leading_attribute() == attr_name


@dataclass
class Catalog:
    buffer_blocks: int          # M
    tables: Dict[str, Table] = field(default_factory=dict)

    def table(self, name: str) -> Table:
        t = self.tables.get(name)
        if t is None:
            raise CatalogError(
                f"nepoznata tabela '{name}'. Poznate tabele: "
                f"{', '.join(sorted(self.tables)) or '(nema)'}"
            )
        return t

    def has_table(self, name: str) -> bool:
        return name in self.tables

    @classmethod
    def from_json_file(cls, path: str) -> "Catalog":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    @classmethod
    def from_dict(cls, data: dict) -> "Catalog":
        try:
            buffer_blocks = int(data["bufferBlocks"])
        except (KeyError, TypeError, ValueError):
            raise CatalogError("ulazni JSON mora sadržati ceo broj 'bufferBlocks'")
        if buffer_blocks < 3:
            # ispod 3 bloka ni jedan algoritam nema smisla (ulaz+ulaz+izlaz)
            raise CatalogError(
                f"bufferBlocks mora biti >= 3 (dato {buffer_blocks}); algoritmi "
                "spajanja i sortiranja zahtevaju bar tri okvira"
            )

        tables: Dict[str, Table] = {}
        for t in data.get("schema", {}).get("tables", []):
            table = cls._table_from_dict(t)
            if table.name in tables:
                raise CatalogError(f"duplirana tabela '{table.name}' u šemi")
            tables[table.name] = table
        if not tables:
            raise CatalogError("šema ne sadrži nijednu tabelu")

        return cls(buffer_blocks=buffer_blocks, tables=tables)

    @staticmethod
    def _table_from_dict(t: dict) -> Table:
        name = _req(t, "name", "tabela")

        attrs = [
            Attribute(
                name=_req(a, "name", f"atribut tabele '{name}'"),
                type=_req(a, "type", f"atribut tabele '{name}'").upper(),
                unique=bool(a.get("unique", False)),
                distinct_values=int(a.get("distinctValues", 1)),
            )
            for a in t.get("attributes", [])
        ]
        for a in attrs:
            if a.type not in ALL_TYPES:
                raise CatalogError(
                    f"{name}.{a.name}: nepoznat tip '{a.type}' "
                    f"(podržani: {', '.join(sorted(ALL_TYPES))})"
                )

        indexes = []
        known = {a.name for a in attrs}
        for ix in t.get("indexes", []):
            kind = _req(ix, "type", f"indeks tabele '{name}'").upper()
            if kind not in (B_PLUS_TREE, HASH):
                raise CatalogError(
                    f"{name}: indeks '{ix.get('name')}' ima nepoznat tip '{kind}'"
                )
            index = Index(
                name=_req(ix, "name", f"indeks tabele '{name}'"),
                attributes=list(_req(ix, "attributes", f"indeks tabele '{name}'")),
                kind=kind,
                clustered=bool(ix.get("clustered", False)),
                tree_height=int(ix.get("treeHeight", 0)),
            )
            for col in index.attributes:
                if col not in known:
                    raise CatalogError(
                        f"{name}: indeks '{index.name}' referencira nepoznat "
                        f"atribut '{col}'"
                    )
            indexes.append(index)

        return Table(
            name=name,
            row_count=int(_req(t, "rowCount", f"tabela '{name}'")),
            block_count=int(_req(t, "blockCount", f"tabela '{name}'")),
            rows_per_block=int(_req(t, "rowsPerBlock", f"tabela '{name}'")),
            attributes=attrs,
            indexes=indexes,
        )


def _req(d: dict, key: str, context: str):
    # obavezno polje - bolja poruka nego goli KeyError
    if key not in d:
        raise CatalogError(f"{context}: nedostaje obavezno polje '{key}'")
    return d[key]
