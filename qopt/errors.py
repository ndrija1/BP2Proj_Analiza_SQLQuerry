class QoptError(Exception):
    pass


class LexError(QoptError):
    pass


class ParseError(QoptError):
    pass


class SemanticError(QoptError):
    pass


class CatalogError(QoptError):
    pass


def point_at(text: str, position: int) -> str:
    """Vrati liniju upita + strelicu ^ ispod znaka na datoj poziciji."""
    line_start = text.rfind("\n", 0, position) + 1
    line_end = text.find("\n", position)
    if line_end == -1:
        line_end = len(text)
    line = text[line_start:line_end]
    return f"{line}\n{' ' * (position - line_start)}^"
