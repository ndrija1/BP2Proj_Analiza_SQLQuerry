from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .errors import CatalogError

NUMERIC_TYPES = {"INT", "DOUBLE","DECIMAL"}
ALL_TYPES = {"INT", "DOUBLE", "STRING", "DATE","DECIMAL"}

B_PLUS_TREE = "B_PLUS_TREE"
HASH = "HASH"
BITMAP="BITMAP"


def is_numeric(type_name: str) -> bool:
    return type_name in NUMERIC_TYPES


def types_comparable(a: str, b: str) -> bool:
    if a == b:
        return True
    return is_numeric(a) and is_numeric(b)


@dataclass
class Attribute:
    name: str
    type: str
    unique: bool = False
    distinct_values: int = 1


@dataclass
class Index:
    name: str
    attributes: List[str]
    kind: str
    clustered: bool = False
    tree_height: int = 0

    @property
    def is_tree(self) -> bool:
        return self.kind == B_PLUS_TREE

    @property
    def is_hash(self) -> bool:
        return self.kind == HASH

    @property
    def is_bitmap(self) -> bool:
        return self.kind == BITMAP


    def leading_attribute(self) -> str:
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
    row_count: int
    block_count: int
    rows_per_block: int
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
    buffer_blocks: int
    tables: Dict[str, Table] = field(default_factory=dict)

    def table(self, name: str) -> Table:
        t = self.tables.get(name)
        if t is None:
            raise CatalogError(
                f" Nepoznata tabela '{name}'. Poznate tabele: "
                f"{', '.join(sorted(self.tables)) or '(nema poznatiih)'}"
            )
        return t

    def has_table(self, name: str) -> bool:
        return name in self.tables


    @classmethod
    def from_dict(cls, data: dict) -> "Catalog":
        try:
            buffer_blocks = int(data["bufferBlocks"])
        except (KeyError, TypeError, ValueError):
            raise CatalogError(" Ulazni JSON mora sadržati ceo broj 'bufferBlocks'")
        if buffer_blocks < 3:
            raise CatalogError(
                f" bufferBlocks mora biti >= 3 (dato {buffer_blocks}); algoritmi "
                "spajanja i sortiranja zahtevaju bar tri okvira, inace nema smisla"
            )

        tables: Dict[str, Table] = {}
        for t in data.get("schema", {}).get("tables", []):
            table = cls._table_from_dict(t)
            if table.name in tables:
                raise CatalogError(f"Duplo definisana tabela '{table.name}' u šemi")
            tables[table.name] = table
        if not tables:
            raise CatalogError(" Sema ne sadrži nijednu tabelu")

        return cls(buffer_blocks=buffer_blocks, tables=tables)

    @classmethod
    def from_json_file(cls, path: str) -> "Catalog":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))


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
        for atribut in attrs:
            if atribut.type not in ALL_TYPES:
                raise CatalogError(
                    f"{name}.{atribut.name}: nepoznat tip '{atribut.type}' "
                    f"( Podrzani: {', '.join(sorted(ALL_TYPES))})"
                )

        indexes = []
        known = {a.name for a in attrs}
        for indeks in t.get("indexes", []):
            kind = _req(indeks, "type", f"indeks tabele '{name}'").upper()
            if kind not in (B_PLUS_TREE, HASH,BITMAP):
                raise CatalogError(
                    f"{name}: indeks '{indeks.get('name')}' ima nepoznat tip indeksa - '{kind}'"
                )
            index = Index(
                name=_req(indeks, "name", f"indeks tabele '{name}'"),
                attributes=list(_req(indeks, "attributes", f"indeks tabele '{name}'")),
                kind=kind,
                clustered=bool(indeks.get("clustered", False)),
                tree_height=int(indeks.get("treeHeight", 0)),
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
    if key not in d:
        raise CatalogError(f"{context}: nedostaje obavezno polje '{key}'")
    return d[key]
