# Procenjivač cene SQL upita (BP2 projekat)

Sistem koji za **SQL upit** i **JSON opis šeme baze sa statistikom** radi sledeće:

1. proverava upit **sintaksno** i **semantički**,
2. bira **najbolji plan evaluacije**,
3. procenjuje njegovu **cenu u broju blok transfera**, i
4. za svaku operaciju prikazuje **izabrani algoritam** i njegovu cenu.

Podržava selekcije (jednakost, poređenje, složeni AND/OR uslovi), B+-stablo i
heš indekse, spoljno objedinjeno sortiranje, sve standardne algoritme spajanja
(ugnežđena petlja, blok ugnežđena, indeks ugnežđena, objedinjeno/sort-merge,
heš, kao i konjunktivni i disjunktivni uslovi spajanja), projekciju, agregaciju,
DISTINCT i skupovne operacije UNION / INTERSECT / EXCEPT. Upiti mogu obuhvatiti
najviše četiri tabele i ne sadrže podupite. Ceo izraz se izračunava po modelu
**materijalizacije**.

---

## 1. Pokretanje

Bez instalacije i bez biblioteka treće strane — samo Python 3 (rađeno na 3.13).

```bash
# jedan upit iz komandne linije
python main.py --query "SELECT * FROM Student WHERE prosek > 9"

# cela grupa primera (po jedan plan za svaki upit)
python main.py --query-file queries/all_examples.sql

# druga datoteka šeme (podrazumevano je test.json)
python main.py --schema test.json --query-file queries/all_examples.sql

# sa standardnog ulaza
echo "SELECT ime FROM Student WHERE smer = 'RTI'" | python main.py

# sakrij linije "isprobani algoritmi" ili ispiši i AST stablo
python main.py --query "..." --no-alternatives
python main.py --query "..." --debug
```

Testovi se pokreću sa `python tests/test_cost_estimator.py` (ili `pytest tests/`).

> Napomena za Windows: izlaz koristi srpska slova (č, ć, š, ž, đ). `main.py`
> postavlja izlaz na UTF-8, pa nikada ne puca; ako se u zastareloj konzoli slova
> pogrešno prikažu, pokrenite `chcp 65001` ili koristite Windows Terminal.

---

## 2. Zašto Python i šta čitati prvo

Python je izabran jer tekst projekta ostavlja slobodu izbora jezika, a prioriteti
ocenjivanja su **čitljivost, lakoća otkrivanja grešaka i lakoća izmene**. Python
ne traži korak prevođenja, čita se skoro kao pseudokod i dozvoljava da formule
cene budu u prvom planu umesto suvišnog koda.

Kod je pravolinijski lanac obrade; module čitati ovim redom:

| Faza | Datoteka | Odgovornost |
|------|----------|-------------|
| katalog | `qopt/catalog.py` | učitava šemu + statistiku + veličinu bafera iz JSON-a |
| lekser | `qopt/lexer.py` | string upita → tokeni |
| parser | `qopt/parser.py` | tokeni → AST (provera **sintakse**) |
| AST | `qopt/ast_nodes.py` | tipovi čvorova + podržana gramatika |
| semantika | `qopt/semantics.py` | da li su tabele/kolone/tipovi/agregati ispravni (provera **semantike**) |
| statistika | `qopt/statistics.py` | procena veličina rezultata (kardinalnosti) |
| model cene | `qopt/cost_model.py` | udžbeničke formule cene u blok transferima (čiste funkcije) |
| fizički | `qopt/physical.py` | fizičke operacije (algoritmi) + stablo plana |
| optimizator | `qopt/optimizer.py` | izbor pristupnog puta + redosled spajanja → najjeftiniji plan |
| prikaz | `qopt/explain.py` | ispisuje plan, algoritme i cene |
| CLI | `main.py` | povezivanje + obrada argumenata |
| greške | `qopt/errors.py` | jedan izuzetak po fazi (jasne poruke o greškama) |

Svaka faza ima jednu odgovornost i mali, dokumentovan interfejs, pa je izmena
gotovo uvek lokalna za jednu datoteku.

---

## 3. Model cene

* **Jedinica:** broj **blok transfera** (projekat traži upravo to, pa se
  pomeranja glave — seek — namerno ne broje).
* **Referenca:** Silberschatz, Korth, Sudarshan — *Database System Concepts*,
  poglavlja 12–13. Svaka formula živi u `qopt/cost_model.py` kao čista funkcija
  običnih brojeva, pa se proverava u odnosu na udžbenik za nekoliko sekundi i
  menja na tačno jednom mestu.
* **Oznake:** `n_r` redova, `b_r` blokova, `f_r` redova/blok, `V(A)` različitih
  vrednosti, `M` blokova bafera, `HT` visina B+-stabla, `SC` = `n_r / V(A)`.

Pristupni putevi selekcije:

| Algoritam | Cena (blok transferi) |
|-----------|-----------------------|
| linearno pretraživanje | `b_r` (jednakost po ključu: `ceil(b_r/2)`) |
| binarno pretraživanje (datoteka sortirana po atributu) | `ceil(log2(b_r)) + ceil(SC/f_r) - 1` |
| B+-stablo, jednakost po ključu | `HT + 1` |
| B+-stablo, sortirajuće, jednakost po ne-ključu | `HT + ceil(SC/f_r)` |
| B+-stablo, sekundarno, jednakost po ne-ključu | `HT + SC` |
| B+-stablo, sortirajuće, opseg | `HT + ceil(pogodaka/f_r)` |
| B+-stablo, sekundarno, opseg | `HT + broj_pogođenih_slogova` |
| heš, jednakost po ključu | `1 + 1` |
| heš, jednakost po ne-ključu | `1 + SC` |

Spajanja (spolja `r`, unutra `s`):

| Algoritam | Cena |
|-----------|------|
| ugnežđena petlja | `b_r + n_r * b_s` |
| blok ugnežđena petlja | `b_r + ceil(b_r/(M-2)) * b_s` |
| indeks ugnežđena petlja | `b_r + n_r * (jedna pretraga indeksa nad s)` |
| objedinjeno (sort-merge) | `b_r + b_s` (+ sortiranje svakog nesortiranog ulaza) |
| heš | `3*(b_r + b_s)`, ili rekurzivno particionisanje ako je strana za izgradnju velika |

Spoljno objedinjeno sortiranje: `b * (2 * broj_prolaza_spajanja + 1)`, gde je
`broj_prolaza_spajanja = ceil(log_{M-1}(ceil(b/M)))` (završni upis dodaje pravilo
materijalizacije ispod).

### Materijalizacija i način sabiranja ukupne cene

Projekat zahteva izračunavanje **materijalizacijom**: svaka operacija čita ulaze
sa diska i upisuje rezultat nazad. To modelujemo uniformno:

* formula poput `b_r + ... * b_s` već broji **čitanje** ulaza, pa **upis**
  svakog rezultata dodajemo tačno jednom: `upis = broj izlaznih blokova`;
* **bazne tabele su listovi** koji su već na disku — koštaju 0 i čita ih roditelj;
* rezultat **najgornje** operacije se isporučuje korisniku, pa se njen upis ne
  naplaćuje. Tako plan sa jednom operacijom ostaje jednak udžbeničkoj vrednosti
  (npr. pretraga po ključu sortirajućeg stabla je `HT+1`, ne `HT+2`), a svaki
  materijalizovani međurezultat se i dalje naplaćuje.

`UKUPNA PROCENJENA CENA` je zbir doprinosa svih čvorova. `EXPLAIN` prikaz za svaki
čvor pokazuje `cena +X (zbir=Y)`, gde je `X` doprinos tog čvora (obrada + upis), a
`Y` tekući zbir podstabla, pa je celo računanje proverljivo.

---

## 4. Procena veličine rezultata (kardinalnosti)

Veličine se penju uz plan u `qopt/statistics.py` po standardnim pravilima:

* jednakost `A = c`: selektivnost `1/V(A)` (dakle `1/n_r`, tj. jedan red, ako je
  `A` ključ); `A <> c`: `1 - 1/V(A)`; opseg: `1/3` (nema histograma);
* `cond1 AND cond2`: `s1 * s2`; `cond1 OR cond2`: `s1 + s2 - s1*s2` (pretpostavka
  nezavisnosti);
* ekvi-spajanje `r.A = s.B`: `n_r * n_s / max(V(A,r), V(B,s))`; Dekartov proizvod:
  `n_r * n_s`;
* torka spajanja je nadovezivanje dve ulazne, pa je šira — faktor blokiranja
  rezultata se kombinuje harmonijski: `1/f = 1/f_r + 1/f_s`, a
  `blokova = ceil(redova / f)`.

Pravila su namerno jednostavna i svako je u jednoj kratkoj funkciji, pa je zamena
(npr. opsezi zasnovani na histogramima) izmena jedne funkcije.

---

## 5. Optimizator

`qopt/optimizer.py` koristi klasičan pristup u stilu **System-R**:

1. razdvoji `WHERE` na konjunkte;
2. klasifikuj svaki konjunkt kao **selekciju** (jedna tabela) ili **uslov
   spajanja** (dve ili više tabela);
3. izaberi najjeftiniji **pristupni put** za svaku baznu tabelu;
4. nabroj redoslede spajanja **dinamičkim programiranjem nad podskupovima
   tabela**: za svaki način deljenja podskupa na dve povezane polovine, isprobaj
   svaki algoritam spajanja u oba redosleda spolja/unutra i zadrži najjeftiniji
   plan po podskupu (sa najviše 4 tabele ovo je maleno i optimalno za naš model);
5. dodaj GROUP BY / agregaciju, HAVING, projekciju / DISTINCT i ORDER BY na vrh,
   logičkim redosledom SQL-a.

**Konjunktivna i disjunktivna spajanja proizlaze iz pravila primenljivosti:**
spajanje čiji je uslov konjunkcija jednakosti može koristiti heš / objedinjeno /
indeks spajanje nad kombinovanim ključem (ostali predikati se filtriraju u
memoriji bez dodatne cene u blokovima); disjunktivan (`OR`) ili ne-ekvi uslov ne
daje ključ ekvi-spajanja, pa optimizator automatski pada na (blok) ugnežđenu
petlju, koja izračunava proizvoljan predikat. Linija „isprobani algoritmi“ u
`EXPLAIN` prikazu pokazuje celo poređenje.

---

## 6. Ulazni JSON format

```jsonc
{
  "bufferBlocks": 10,            // M, veličina bafera u blokovima
  "schema": {
    "tables": [{
      "name": "Student",
      "rowCount": 1000,           // n_r
      "blockCount": 100,          // b_r
      "rowsPerBlock": 10,         // f_r
      "attributes": [
        { "name": "indeks", "type": "STRING", "unique": true, "distinctValues": 1000 }
      ],
      "indexes": [{
        "name": "idx_student_indeks_clustered",
        "attributes": ["indeks"],
        "type": "B_PLUS_TREE",    // ili "HASH"
        "clustered": true,         // da li je datoteka sortirana po ovim atributima?
        "treeHeight": 3            // HT (samo za B+-stablo)
      }]
    }]
  }
}
```

Podržani tipovi: `INT`, `DOUBLE`, `STRING`, `DATE`.

---

## 7. Podržana SQL gramatika

Ažurirani tekst projekta definiše **zagarantovani oblik upita**: SELECT lista
isključivo od atributa, FROM sa tabelama razdvojenim zarezima (bez JOIN
operatora), WHERE kao konjunkcija od najviše 6 uslova, ORDER BY sa najviše jednim
atributom, bez GROUP BY.

```
upit          := select_jezgro ( (UNION|INTERSECT|EXCEPT) [ALL] select_jezgro )*
select_jezgro := SELECT [DISTINCT] lista_atributa
                 FROM tabela (',' tabela)*          -- samo zarezi, bez JOIN
                 [WHERE uslov]                        -- konjunkcija <= 6 uslova
                 [ORDER BY atribut [ASC|DESC]]        -- najviše 1 atribut
lista_atributa := '*' | (atribut [AS alias]) (',' ...)*
uslov         := or_term (OR or_term)*
or_term       := faktor (AND faktor)*
faktor        := poređenje | '(' uslov ')' | NOT faktor
poređenje     := operand (= | <> | != | < | <= | > | >=) operand
operand       := atribut | literal
```

Lista traženih *algoritama* u tekstu (nepromenjena) i dalje uključuje agregaciju
i skupovne operacije, pa parser dodatno prihvata `GROUP BY` / `HAVING`, agregatne
funkcije u SELECT-u i ulančavanje `UNION / INTERSECT / EXCEPT` (videti odeljak 8).

---

## 8. Pretpostavke (tekst projekta ih izričito poziva)

* **Cena = samo blok transferi** (kako je traženo); seek se ne broji.
* **Model cene** je Silberschatz; **selektivnost opsega je podrazumevano 1/3**, a
  predikati se smatraju **nezavisnim** (nema histograma u ulazu).
* **Materijalizacija** kako je opisano u odeljku 3 (upis krajnjeg rezultata se ne
  naplaćuje).
* SQL **ključne reči su neosetljive na velika/mala slova**; **identifikatori su
  osetljivi** i moraju odgovarati šemi (koja koristi camelCase, npr.
  `studentIndeks`).
* `STRING` literal upoređen sa `DATE` kolonom tretira se kao `DATE`.
* **Najviše 4 tabele** po SELECT bloku; **bez podupita**.
* **Princip usaglašavanja sa ažuriranim tekstom:** poštujemo svako novo
  ograničenje gramatike osim kada bi to uklonilo algoritam koji (nepromenjena)
  lista traženih algoritama i dalje zahteva. Zato:
  * `JOIN` operator je uklonjen iz FROM (spajanja se i dalje dobijaju iz liste sa
    zarezima + uslova u WHERE, pa nijedan algoritam nije izgubljen);
  * `WHERE` je ograničen na **konjunkciju od najviše 6 uslova** (uslov sme da
    sadrži OR, zbog složenih uslova i disjunktivnih spajanja iz liste algoritama);
  * `ORDER BY` je ograničen na **1 atribut**;
  * **agregacija / GROUP BY / HAVING** i **skupovne operacije** su zadržane jer
    su izričito u listi traženih algoritama (iako ih zagarantovani oblik upita ne
    koristi); ako se traži doslovno odbijanje, ograničenje je trivijalno dodati u
    `semantics.py`.
* Spajanje indeks ugnežđenom petljom pretražuje indeks nad **baznom** tabelom kao
  unutrašnjom relacijom.
* Veličine rezultata skupovnih operacija koriste jednostavne, dokumentovane
  granice (nema podataka o preklapanju vrednosti).
* Složeni indeks je upotrebljiv samo na **levom prefiksu** svojih atributa.

---

## 9. Kako menjati / proširiti

Razdvajanje odgovornosti čini uobičajene izmene izmenama jedne datoteke:

* **Podesi formulu cene** → `qopt/cost_model.py` (čiste funkcije, po jedna).
* **Promeni pravilo veličine/selektivnosti** → `qopt/statistics.py`.
* **Dodaj novi tip indeksa ili podatka** → konstanta + grana u `qopt/catalog.py`,
  pa obrada tamo gde se grade pristupni putevi u `qopt/physical.py`.
* **Dodaj algoritam spajanja/selekcije** → graditelj u `qopt/physical.py` i jedna
  linija među kandidatima u `qopt/optimizer.py` (`_best_join` ili
  `selection_candidates`).
* **Proširi SQL gramatiku** → čvor u `qopt/ast_nodes.py`, pravilo u
  `qopt/parser.py` i provera u `qopt/semantics.py`.
* **Promeni izlaz** → samo `qopt/explain.py`.

---

## 10. Mapa datoteka

```
BP2_projekat_25_26.pdf        tekst zadatka (original)
zadatak_tekst.txt             izvučen tekst zadatka (srpski)
test.json                     primer šeme + statistike + veličine bafera
main.py                       ulazna tačka (CLI)
queries/all_examples.sql      20 primera upita koji pokrivaju sve funkcionalnosti
tests/test_cost_estimator.py  skup testova bez zavisnosti
qopt/                         implementacija (videti tabelu u odeljku 2)
```
