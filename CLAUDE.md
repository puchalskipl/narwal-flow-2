# narwal-flow-2 — zasady pracy

- Integracja HA tylko dla Narwal Flow 2 (klucze produktu w `custom_components/narwal/const.py`). Kod dla innych modeli nie wraca.
- `narwal_client/` i `custom_components/narwal/narwal_client/` muszą być identyczne (test `test_integration_structure`).
- Testy: venv z `requirements-dev.txt`, `python -m pytest -q`. Przed wgraniem do HA testy zielone.
- Wgrywanie: najpierw testowe HA (LXC 900), produkcja dopiero po zgodzie właściciela. Push do GitHuba tylko za zgodą.
- Kontrakty, których nie łamać bez poprawy konsumentów w repo HA: usługa `narwal.clean_rooms` (pola `rooms`, `mode`, `suction`, `water`, `mop_strength`, `passes`, `mop_passes`, `route`), `unique_id` encji, domena `narwal`.
- Komentarze i dokumentacja po polsku, bez średników w tekstach. Komunikaty błędów usług HA zostają po angielsku do czasu tłumaczeń wyjątków.
- Bez PR do repozytorium autora (upstream `sjmotew/NarwalIntegration`), poprawki autora bierzemy przez `git fetch upstream` i cherry-pick.
