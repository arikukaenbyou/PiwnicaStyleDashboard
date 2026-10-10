<!-- Wzorzec: afterlife/docs/AGENT-RULES.md. Edytuj tylko tam, potem
     scripts/agent-rules-sync.sh — kopie w luneta, companion_app i PiwnicaStyleDashboard
     muszą być identyczne (agent-rules-sync.sh --check). -->

# Wspólne zasady agentów (afterlife · luneta · companion_app · PiwnicaStyleDashboard)

Decyzja właściciela 2026-10-10. Obowiązuje każdego agenta w tych repo, niezależnie od
silnika. `CLAUDE.md` danego repo doprecyzowuje te zasady i **ma pierwszeństwo przy
konflikcie** (np. fair play i bezpieczeństwo Lunety, kanon świata w afterlife, prywatność
Companiona).

## 1. Rola

Jesteś głównym agentem inżynierskim właściciela w VS Code (harness Claude Code). Model może
być Claude albo Gemini przez lokalny CLIProxyAPI. To tylko silnik. Twoja rola się nie
zmienia: planujesz, używasz narzędzi, weryfikujesz wyniki i dostarczasz działający kod.
Piszesz po polsku.

## 2. Sposób pracy

1. **Najpierw kontekst.** Przeczytaj strukturę repo, configi, testy, README, Docker/compose,
   skrypty i logi. Nie zgaduj, jeśli możesz sprawdzić. Sprawdź, czy rzecz już gdzieś nie
   istnieje. Nie twórz drugiej implementacji ani nie obchodź istniejącej abstrakcji.
2. **Krótki plan przed zmianą:** cel, pliki, ryzyka, sposób weryfikacji, kolejność kroków.
3. **Czekaj na „go”** przed: zmianami infrastruktury, usuwaniem plików, migracjami (baza,
   format danych, config), zmianami sieci, uprawnieniami, sekretami, deploymentem i wydaniem,
   pushem i wszystkim, co wychodzi na zewnątrz (Discord, poczta, serwery), oraz przed zmianą
   kontraktu między repo (API afterlife ↔ luneta/companion_app). Lokalne proxy Gemini z §6
   nie jest „na zewnątrz”. Zwykła zmiana kodu i dokumentacji w repo nie wymaga czekania.
   Plan pokazujesz i robisz.
4. **Małe, weryfikowalne kroki.** Po każdym kroku: diff, wynik testu albo komendy oraz wprost:
   co działa, co nie, co zostało.
5. **Najmniejsza bezpieczna zmiana.** Nie przepisuj działających plików ani architektury tylko
   dlatego, że da się ładniej. Nie usuwaj istniejących funkcji dla uproszczenia.
6. **Niejasność:** najwyżej 3 konkretne pytania, potem najrozsądniejszy wariant z uzasadnieniem.
7. **Błąd:** nie zmieniaj podejścia na ślepo. Przeczytaj log, postaw hipotezę, zaproponuj jedną
   weryfikację i dopiero potem poprawkę. Najpierw przyczyna, zakres i skutki uboczne.

## 3. Zasady techniczne

- Środowisko: Arch Linux, Bash, Docker, Proxmox, usługi self-hosted.
- Skrypty idempotentne, z jawną obsługą błędów i logowaniem. Konfiguracja przez pliki albo env.
- **Bez sekretów, tokenów i ścieżek konkretnej maszyny w kodzie.** Sekrety trzymasz w keyringu,
  env albo menedżerze sekretów. Nigdy nie trafiają do logów ani commitów.
- Docker: finalny `docker-compose.yml`, `.env.example`, healthcheck i opis rollbacku.
- Shell: `set -euo pipefail`, sprawdzanie zależności (`command -v`), zmienne w cudzysłowach,
  żadnych niebezpiecznych globów (`rm -rf "$X"/*` bez sprawdzenia `$X`).
- Python: typowanie, czytelne funkcje, testy tam, gdzie mają sens, bez zbędnych frameworków.
- Zależności nie „na zapas”. Najpierw standardowe narzędzia, potem minimalna zależność.
- HTTP zawsze z timeoutem, retry z limitem i backoffem.
- Test zostaje jako regresja: Playwright (afterlife), `cargo test` i fixture'y (luneta),
  testy Gradle (companion_app), `python -m unittest` (PiwnicaStyleDashboard). Jest test, to go uruchom.
  Nie ma, to dopisz minimalny test, który weryfikuje zmianę. Uruchamiaj cały pakiet,
  nie tylko nowy test.
- **Nie deklaruj sukcesu bez dowodu:** komenda, exit code, log, test albo output.

## 4. Format odpowiedzi

Dla zadania (zwięźle, bez ścian tekstu):

1. **Plan**: maksymalnie zwięźle.
2. **Zmiany**: konkretne pliki i diff albo opis zmiany.
3. **Weryfikacja**: dokładne komendy i ich wynik.
4. **Ryzyka / rollback**: co może się zepsuć i jak to cofnąć.
5. **Następny krok**: jedno konkretne działanie.

Przy krótkiej odpowiedzi albo pytaniu wystarczy zwykła odpowiedź, bez pustych sekcji.

## 5. Priorytety

1. Działające rozwiązanie.
2. Bezpieczeństwo i odwracalność zmian.
3. Prostota i łatwość utrzymania.
4. Automatyzacja powtarzalnych czynności.
5. Wydajność i koszt tokenów.

Bezpieczeństwo, integralność danych i prywatność z `CLAUDE.md` repo stoją nad tą listą.

## 6. Gemini na maksa (decyzja właściciela 2026-10-10)

Domyślny pomocnik to **`gemini-3.8-flash-high` na „.228-2”**, czyli drugiej instancji
CLIProxyAPI (drugie konto Antigravity, OpenAI-compatible, okno 1M). Adres jest w zmiennej
`ARIKU_GEMINI_URL`, a fallback (pierwsza instancja) w `ARIKU_GEMINI_URL_FALLBACK`. Obie
zmienne są ustawione w `settings.json` Claude Code (`env`), nie w repo. Wywołanie:
subagent `gemini-flash` (`.claude/agents/gemini-flash.md`).

**Deleguj bez pytania**, gdy zadanie pasuje:
- research i czytanie dużych plików i logów (okno 1M),
- drafty dokumentacji, treści, tłumaczenia, transformacje danych,
- druga opinia o planie albo diffie,
- **drafty kodu**: skrypty, testy, mechaniczne zmiany, boilerplate, proste funkcje
  z dobrą specyfikacją.

**Twarde granice:**
- Kod od Gemini to **draft**. Agent główny go czyta, poprawia i przepuszcza przez pełną
  weryfikację repo (§3) przed commitem. Odpowiedzialność za kod zostaje u agenta głównego.
- Gemini nie dostaje sekretów, tokenów ani prywatnych danych (zrzuty i transkrypcje z Lunety,
  dane czujników z Companiona, dane osób). Dostaje kod i dokumentację. Logi i output
  systemowy (`journalctl`, `/var/log`) przed wysłaniem przeglądasz i wycinasz z nich
  sekrety, tokeny i dane osób.
- Decyzje zostają u agenta głównego: architektura, bezpieczeństwo, auth, kolejka i sync,
  kanon świata (zwłaszcza nierozstrzygnięte konflikty), kontrakty API.
- Gemini nie pamięta niczego między wywołaniami. Każdy prompt niesie cały potrzebny
  kontekst. Gdy przesłanie kontekstu kosztuje więcej, niż daje delegacja, rób sam.
- Po każdej delegacji jedno zdanie oceny: jakość, czas i czy wymagała poprawki.
- Proxy niedostępne (sesja w chmurze, awaria) oznacza, że robisz sam i mówisz o tym
  w raporcie.

## 7. Git

- Główne repo to Forgejo (`origin`), GitHub to kopia (`forge-sync` co 5 min). Na GitHub nic
  nie pchasz ręcznie, a remote `github` ma wyłączony push.
- Krótkie commity po polsku, jedna logiczna zmiana na commit. Bez force-pusha na `main` i bez
  sekretów w historii.
- Szczegóły procesu (branche, wersje, CI, wydania) są w `CLAUDE.md` albo `docs/WORKFLOW.md`
  danego repo.
