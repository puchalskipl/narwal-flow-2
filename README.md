# Narwal Flow 2 — integracja Home Assistant

Lokalna integracja robota sprzątającego **Narwal Flow 2** (WebSocket, port 9002, bez chmury).
Fork [sjmotew/NarwalIntegration](https://github.com/sjmotew/NarwalIntegration) okrojony do jednego modelu
i dostosowany do użycia w jednym domu: pokoje w kolejności, ustawienia per tryb, osobne cykle
odkurzania i mopowania, mapa w stylu aplikacji.

Domena pozostaje `narwal`, więc wpis integracji, `entity_id` i historia z wersji autora zostają.

## Co daje

- Encja `vacuum`: start (cały dom), stop, pauza, powrót, lokalizacja, ssanie (4 poziomy Flow 2),
  `vacuum.clean_area` po przypisaniu pokoi do obszarów HA.
- Usługa `narwal.clean_rooms`: pokoje w kolejności, tryb (odkurzanie, mopowanie, odkurzanie przed mopowaniem,
  odkurzanie z mopowaniem), ssanie, wilgotność, siła szorowania, cykle, osobne cykle mopowania (`mop_passes`),
  dokładność trasy.
- Usługa `narwal.get_current_task`: bieżące zadanie robota (diagnostyka protokołu).
- Czujniki: bateria, ładowanie, powierzchnia, czas, pozostały czas, firmware, wynik ostatniego sprzątania,
  stan worka, detergent (domyślnie wyłączony, na Flow 2 nie działa).
- Czujniki binarne: w stacji, błąd (z kodami), konserwacja, wymiana, zbiorniki i worki stacji.
- Zadania stacji jako przełączniki: opróżnianie pojemnika, mycie mopa, suszenie mopa, suszenie
  i dezynfekcja pojemnika oraz worka.
- Światło stacji (tryby jak w aplikacji).
- Kamera mapy (MJPEG) z pozycją robota, stacją i śladem przejazdu.

## Instalacja

HACS → własne repozytorium `puchalskipl/narwal-flow-2` (integracja), pobranie, restart HA.
Robot jest wykrywany przez mDNS/DHCP albo dodawany po adresie IP. Obsługiwane klucze produktu:
`QxMSPG6VSO`, `iSuVlI1If2`, `mkbqaprvrb`. Inny model zostaje odrzucony.

## Rozwój

- `narwal_client/` (czysta biblioteka protokołu) i `custom_components/narwal/narwal_client/` muszą być identyczne.
- Testy: `python -m venv venv && venv/bin/pip install -r requirements-dev.txt && venv/bin/python -m pytest -q`.
- Opis protokołu: [docs/PROTOCOL.md](docs/PROTOCOL.md), zadania stacji: [docs/DOCK_TASKS.md](docs/DOCK_TASKS.md).

Licencja MIT (jak w repozytorium autora).
