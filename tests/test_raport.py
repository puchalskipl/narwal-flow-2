"""Raport zakończonego zadania (report/clean_report), układ pól z raportu Flow 2 z 2026-10-26."""

from __future__ import annotations

import struct

from narwal_client.raport import raport_sprzatania


def _f(v: float) -> int:
    """float32 jako surowe uint32, tak jak oddaje je blackboxprotobuf."""
    return struct.unpack("I", struct.pack("f", v))[0]


def test_raport_z_jedna_przeszkoda() -> None:
    dane = {
        "2": _f(3.456), "3": 269, "6": 1793012346, "7": 1793012634,
        "12": {"6": {"1": -189, "2": 109, "3": -61, "4": 168}, "28": "Góra"},
        "16": [{"1": 1, "2": 8, "3": 2}, {"1": 2, "2": 8, "3": 105}],
        # jedna przeszkoda przychodzi jako dict, nie lista
        "31": {"1": {"1": 1, "2": "https://example/1793012585535920722_1552284562_10_1.jpg.nenc",
                     "3": {"1": {}, "2": 480, "3": 480}, "5": {"1": _f(282.0), "2": _f(291.0)}}},
    }
    r = raport_sprzatania(dane)
    assert r["czas_s"] == 269
    assert r["powierzchnia"] == 3.46
    assert r["poczatek"] == 1793012346 and r["koniec"] == 1793012634
    assert r["pokoje"] == [8]
    assert r["mapa"] == "Góra"
    assert [(p["typ"], p["nazwa"], p["czas"]) for p in r["przeszkody"]] == [(1, "inne", 1793012585)]
    assert "x" not in r["przeszkody"][0]  # raport nie podaje położenia na mapie


def test_przeszkody_lista_i_czas() -> None:
    dane = {
        "31": {"1": [
            {"1": 1, "2": "a.jpg.nenc", "3": {"1": {}}},
            {"1": 7, "2": "https://x/1793033947046189931_1134271818_10_1.jpg.nenc", "3": {"1": {}}},
        ]},
    }
    r = raport_sprzatania(dane)
    assert [p["typ"] for p in r["przeszkody"]] == [1, 7]
    assert r["przeszkody"][0]["nazwa"] == "inne"
    assert r["przeszkody"][1]["nazwa"] == "typ 7"
    assert r["przeszkody"][0]["czas"] is None
    assert r["przeszkody"][1]["czas"] == 1793033947


def _floaty(*v: float) -> bytes:
    return struct.pack(f"<{len(v)}f", *v)


def test_polozenie_z_objazdu() -> None:
    # trasa: 2 punkty dojazdu, pętla objazdu (typ 3) wokół (10, 0), powrót
    xs = [-8.0, 0.0, 9.0, 10.0, 11.0, 10.0, 5.0]
    ys = [-1.0, 0.0, 0.0, 1.0, 0.0, -1.0, 0.0]
    dane = {
        "12": {"6": {"1": -189, "3": -61},
               "14": {"1": _floaty(*xs), "2": _floaty(*ys)},
               "31": [{}, {"1": 2, "2": 0}, {"1": 3, "2": 2}, {"1": 1, "2": 6}]},
        "31": {"1": {"1": 1, "2": "a.jpg.nenc"}},
    }
    p = raport_sprzatania(dane)["przeszkody"][0]
    assert (p["x"], p["y"]) == (71.0, 189.0)


def test_bez_polozenia_gdy_objazdow_mniej() -> None:
    dane = {
        "12": {"6": {"1": -189, "3": -61},
               "14": {"1": _floaty(0.0, 1.0), "2": _floaty(0.0, 1.0)},
               "31": [{"1": 2, "2": 0}]},
        "31": {"1": {"1": 1, "2": "a.jpg.nenc"}},
    }
    assert "x" not in raport_sprzatania(dane)["przeszkody"][0]
