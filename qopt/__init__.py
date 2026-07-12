# qopt - procena cene izvrsavanja SQL upita (BP2 projekat)
#
# Tok obrade: lexer -> parser -> semantics -> optimizer (koristi statistics,
# cost_model i physical) -> explain. Ulazna tacka je main.py u korenu.
# Cena se meri u broju blok transfera, evaluacija materijalizacijom.
