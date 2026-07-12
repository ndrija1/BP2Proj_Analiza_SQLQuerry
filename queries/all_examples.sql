-- Primeri upita koji pokrivaju svaku operaciju/algoritam koji projekat zahteva.
-- Pokretanje svih:   python main.py --query-file queries/all_examples.sql
-- Iskazi se razdvajaju sa ';', a linije koje počinju sa '--' su komentari.
--
-- ========================================================================
-- DEO A: upiti u zagarantovanoj gramatici iz teksta projekta
--   (SELECT samo atributi, FROM tabele razdvojene zarezima bez JOIN,
--    WHERE konjunkcija od najviše 6 uslova, ORDER BY najviše 1 atribut,
--    bez GROUP BY)
-- ========================================================================

-- ----------------------------- SELEKCIJA -----------------------------
-- (1) jednakost po ključu -> sortirajuće B+-stablo (HT+1) bije linearno
SELECT indeks, ime FROM Student WHERE indeks = '2020/0123';

-- (2) jednakost po ne-ključu -> heš indeks nad 'ime'
SELECT ime, prosek FROM Student WHERE ime = 'Marko';

-- (3) opseg po ključu sortirajućeg B+-stabla
SELECT naziv FROM Predmet WHERE predmetId > 50;

-- (4) konjunkcija: složeni indeks (smer,prosek) vodi po smer, prosek se filtrira
SELECT ime FROM Student WHERE smer = 'RTI' AND prosek >= 8.5;

-- (5) disjunkcija indeksiranih jednakosti -> unija heš pretraga
SELECT ime FROM Student WHERE ime = 'Marko' OR ime = 'Petar';

-- (6) disjunkcija koju nijedan indeks ne pokriva -> linearno + filter u memoriji
SELECT ime FROM Student WHERE smer = 'RTI' OR prosek > 9.0;

-- ----------------------------- SPAJANJA -----------------------------
-- (7) spajanje po indeksiranom stranom ključu; objedinjeno spajanje pobeđuje
--     jer je Student sortiran po indeks
SELECT s.ime, i.ocena FROM Student s, Ispit i
WHERE s.indeks = i.studentIndeks AND i.ocena = 10;

-- (8) spajanje Ispit-Predmet po predmetId (Predmet sortiran, Ispit velik/nesortiran)
SELECT i.ocena, p.naziv FROM Ispit i, Predmet p WHERE i.predmetId = p.predmetId;

-- (9) konjunktivni uslov spajanja (složeni ključ)
SELECT st.tip, i.ocena FROM Stipendija st, Ispit i
WHERE st.studentIndeks = i.studentIndeks AND st.iznos = i.ocena;

-- (10) disjunktivni uslov spajanja -> rešava (blok) ugnežđena petlja
SELECT s.ime, st.tip FROM Student s, Stipendija st
WHERE s.indeks = st.studentIndeks OR s.smer = st.tip;

-- (11) spajanje tri tabele
SELECT s.ime, p.naziv, i.ocena
FROM Student s, Ispit i, Predmet p
WHERE s.indeks = i.studentIndeks AND i.predmetId = p.predmetId AND i.ocena >= 8;

-- (12) spajanje četiri tabele (maksimum dozvoljen)
SELECT s.ime, p.naziv, i.ocena, st.iznos
FROM Student s, Ispit i, Predmet p, Stipendija st
WHERE s.indeks = i.studentIndeks
  AND i.predmetId = p.predmetId
  AND s.indeks = st.studentIndeks
  AND s.smer = 'RTI';

-- ----------------------- PROJEKCIJA / SORTIRANJE -----------------------
-- (13) projekcija + ORDER BY (1 atribut) -> spoljno objedinjeno sortiranje na vrhu
SELECT ime, prosek FROM Student WHERE prosek > 8.0 ORDER BY prosek DESC;

-- (14) DISTINCT -> eliminacija duplikata
SELECT DISTINCT smer FROM Student;

-- ========================================================================
-- DEO B: operacije koje su i dalje u listi traženih algoritama, iako ih
--   zagarantovana gramatika ne koristi (agregacija, skupovne operacije).
--   Implementacija ih i dalje podržava i procenjuje njihovu cenu.
-- ========================================================================

-- (15) grupisanje + agregati
SELECT smer, COUNT(*), AVG(prosek) FROM Student GROUP BY smer;

-- (16) grupisanje + HAVING
SELECT studentIndeks, COUNT(*) FROM Ispit
GROUP BY studentIndeks HAVING COUNT(*) > 5;

-- (17) skalarni agregat (jedna grupa)
SELECT AVG(prosek), MAX(prosek) FROM Student;

-- (18) UNION dve selekcije
SELECT studentIndeks FROM Ispit WHERE ocena = 10
UNION
SELECT studentIndeks FROM Stipendija WHERE tip = 'REDOVNA';

-- (19) INTERSECT
SELECT studentIndeks FROM Ispit
INTERSECT
SELECT studentIndeks FROM Stipendija;

-- (20) EXCEPT
SELECT indeks FROM Student
EXCEPT
SELECT studentIndeks FROM Stipendija;
