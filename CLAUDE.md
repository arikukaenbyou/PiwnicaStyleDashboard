# PiwnicaStyleDashboard — zasady dla Claude Code

Animowana warstwa PCB z dashboardem systemu dla Arch Linuksa (X11). Python, pakiet
`piwnica_dashboard/`, instalacja `install.sh`, pakiet AUR-owy w `packaging/PKGBUILD`.

@docs/AGENT-RULES.md

- `docs/AGENT-RULES.md` to wspólne zasady agentów (afterlife, luneta, companion_app,
  PiwnicaStyleDashboard). Kopia 1:1 z afterlife (`scripts/agent-rules-sync.sh` tam),
  nie edytuj jej tutaj. Przy konflikcie wygrywa ten plik.
- **Repo jest publiczne.** W kodzie, README i commitach nie ma tokenów ani prywatnych danych.
  Tokeny trzymasz w `~/.config/piwnica-dashboard/*.token` (chmod 600), poza repo.
  Przykłady konfiguracji mają wartości przykładowe.
- Testy: `python -m unittest discover -s tests -t .`, cały pakiet przed commitem.
  Dane sprzętu w testach są z atrap (`tests/fake.py`), nie z maszyny.
- Lekkość: dashboard działa pod grą. Przemalowujesz tylko zmienione obszary, wolne czujniki
  odpytujesz rzadko, a nowy efekt nie może podnieść zużycia CPU bez powodu (zmierz przed
  i po). Bez roota: tylko odczyt `/proc`, `/sys` i logów.
- Panel aktualizacji tylko pokazuje werdykt inwentarza ariku.pl. Dashboard sam niczego nie
  ocenia ani nie aktualizuje.
