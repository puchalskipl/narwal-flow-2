"""Raport zakończonego zadania (`report/clean_report`), Flow 2 v01.09.10.02.

Robot wysyła go na koniec zadania, gdy subskrypcja obejmuje ten temat (aplikacja go
subskrybuje, podsłuch MQTT aplikacji 2026-10-26). Znaczenie pól ustalone na raporcie
z testu Pralni 2026-10-26 (porównanie z czujnikami i aplikacją):
  2 powierzchnia m² (float32), 3 czas sprzątania s, 6 początek, 7 koniec (unix s),
  12 mapa zadania (jak `map/get_map`: 6 = przesunięcie siatki, 28 = nazwa mapy),
  16 przejazdy (pole 2 = pokój), 31.1 (powtarzane) przeszkody z rozpoznawania: 1 typ, 2 zdjęcie
  (zaszyfrowane, `.jpg.nenc`, nazwa pliku zaczyna się czasem zdjęcia w ns), 3.2 i 3.3 rozmiar
  zdjęcia, 4 i 5 punkt obiektu na zdjęciu. Położenia przeszkody na mapie raport wprost nie podaje
  (3.1 puste w obu raportach z 2026-10-26, także z przedmiotem na środku Pralni).
  Trasa zadania: 12.14 (pole 1 = x, pole 2 = y, float32, układ świata w komórkach), jej odcinki:
  12.31 (1 typ, 2 indeks pierwszego punktu). Typ 3 to objazd przeszkody dookoła: środek pętli
  zgadza się z przeszkodą w aplikacji (test Pralni 2026-10-26 18:01). Robot nie zawsze objeżdża
  przeszkodę (przy ścianie nie), dlatego położenie dostaje przeszkoda tylko wtedy, gdy objazdów
  jest tyle samo co przeszkód (kolejność ta sama).
"""

from __future__ import annotations

import struct
from typing import Any

from .models import _to_float32

# typy przeszkód potwierdzone porównaniem z aplikacją (pole 31.x.1)
TYPY_PRZESZKOD: dict[int, str] = {
    1: "inne",
}


def _lista(wartosc: Any) -> list:
    """Powtarzane pole protobuf: jeden element przychodzi jako dict, kilka jako lista."""
    if wartosc is None:
        return []
    return wartosc if isinstance(wartosc, list) else [wartosc]


def _czas_zdjecia(adres: str) -> int | None:
    """Czas zdjęcia (unix s) z nazwy pliku `<ns>_<…>.jpg.nenc`."""
    try:
        return int(adres.rsplit("/", 1)[-1].split("_", 1)[0]) // 1_000_000_000
    except ValueError:
        return None


def _floaty(wartosc: Any) -> list[float]:
    """Spakowane float32 (bajty pola protobuf)."""
    if not isinstance(wartosc, (bytes, bytearray)):
        return []
    n = len(wartosc) // 4
    return list(struct.unpack(f"<{n}f", bytes(wartosc[: n * 4])))


def _objazdy(mapa: dict[str, Any]) -> list[tuple[float, float]]:
    """Środki objazdów przeszkód (odcinki trasy typu 3) w układzie świata, w kolejności trasy."""
    trasa = mapa.get("14") if isinstance(mapa.get("14"), dict) else {}
    xs, ys = _floaty(trasa.get("1")), _floaty(trasa.get("2"))
    odcinki = [
        (int(o.get("1", 0)), int(o["2"]))
        for o in _lista(mapa.get("31")) if isinstance(o, dict) and "2" in o
    ]
    wynik = []
    for (typ, od), (_, do) in zip(odcinki, odcinki[1:] + [(0, len(xs))]):
        px, py = xs[od:do], ys[od:do]
        if typ == 3 and px and len(px) == len(py):
            wynik.append((sum(px) / len(px), sum(py) / len(py)))
    return wynik


def raport_sprzatania(dane: dict[str, Any]) -> dict[str, Any]:
    """Raport z rozkodowanego `report/clean_report` jako prosty słownik."""
    mapa = dane.get("12") if isinstance(dane.get("12"), dict) else {}
    przeszkody = []
    pole31 = dane.get("31") if isinstance(dane.get("31"), dict) else {}
    for p in _lista(pole31.get("1")):
        if not isinstance(p, dict):
            continue
        typ = int(p.get("1", 0))
        zdjecie = str(p.get("2", ""))
        przeszkody.append({
            "typ": typ,
            "nazwa": TYPY_PRZESZKOD.get(typ, f"typ {typ}"),
            "czas": _czas_zdjecia(zdjecie),
            "zdjecie": zdjecie,
        })
    objazdy = _objazdy(mapa)
    if przeszkody and len(objazdy) == len(przeszkody):
        przesuniecie = mapa.get("6") if isinstance(mapa.get("6"), dict) else {}
        ox, oy = int(przesuniecie.get("3", 0)), int(przesuniecie.get("1", 0))
        for p, (x, y) in zip(przeszkody, objazdy):
            # komórka siatki mapy jak stacja i robot (wartość minus przesunięcie)
            p["x"], p["y"] = round(x - ox, 1), round(y - oy, 1)
    pokoje: list[int] = []
    for przejazd in _lista(dane.get("16")):
        if isinstance(przejazd, dict) and "2" in przejazd and int(przejazd["2"]) not in pokoje:
            pokoje.append(int(przejazd["2"]))
    powierzchnia = _to_float32(dane.get("2", 0)) or 0.0
    return {
        "poczatek": int(dane.get("6", 0)),
        "koniec": int(dane.get("7", 0)),
        "czas_s": int(dane.get("3", 0)),
        "powierzchnia": round(powierzchnia, 2),
        "pokoje": pokoje,
        "mapa": mapa.get("28", ""),
        "przeszkody": przeszkody,
    }
