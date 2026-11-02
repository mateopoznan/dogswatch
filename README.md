# Pies Watch

**[English version](README.en.md)**

Pies Watch nasłuchuje kamery, strumienia albo mikrofonu i zapisuje szczekanie psa. Model YAMNet od Google rozpoznaje szczekanie, a aplikacja wycina z dźwięku krótkie nagrania z małym zapasem przed i po. Panel w przeglądarce pokazuje podgląd na żywo, statystyki, tygodniowy wykres godzin, listę nagrań do odsłuchu i eksport CSV/ZIP.

Wszystko działa lokalnie, na Twoim komputerze. Dźwięk nie jest nigdzie wysyłany.

## Co potrafi

- **Źródła dźwięku:** kamera lub rejestrator Dahua, dowolny strumień obsługiwany przez FFmpeg (RTSP, HTTP, RTMP, SRT, czyli większość kamer IP) albo mikrofon podłączony do komputera z Linuksem.
- **Nagrania tylko ze szczekaniem:** głośniejszy dźwięk rozpoczyna nasłuch, a po ciszy model ocenia go co pół sekundy. Zapisywane są fragmenty ze szczekaniem i kilkoma sekundami marginesu. Krótkie przerwy są łączone w jedno nagranie, dłuższe dzielą je na osobne.
- **Trzy osobne miary:** długość nagrania (z przerwami), szczekanie wykryte przez model (szacunek) i epizod potwierdzony ręcznie po odsłuchu.
- **Wykres tygodniowy:** siatka 7 dni × 24 godziny, kolor oznacza ilość szczekania, a po najechaniu widać czas, liczbę zdarzeń i potwierdzenia.
- **Przegląd nagrań:** odsłuch, oznaczanie fałszywych wykryć, ręczne potwierdzanie, usuwanie.
- **Raporty:** CSV i ZIP z nagraniami; miesięczne archiwa po przejrzeniu wszystkich zdarzeń z miesiąca.
- **Ustawienia w panelu:** źródło, progi modelu, marginesy, strefa czasowa, język (polski lub angielski), logowanie.
- **Opcjonalne logowanie:** potrzebne, gdy panel ma być dostępny spoza sieci domowej.

## Wymagania

- Docker z Compose (Docker Desktop na macOS lub Windows, Docker Engine na Linuksie).
- Komputer działający cały czas: Raspberry Pi 4/5, mini PC, NAS z Dockerem albo Mac. Model działa na procesorze; wystarcza 1 GB wolnej pamięci RAM.
- Kamera lub inne źródło przekazujące dźwięk.

## Instalacja

```bash
git clone https://github.com/mateopoznan/dogswatch.git
cd dogswatch
./install.sh
```

Instalator pyta o język, port panelu, strefę czasową, źródło dźwięku i to, czy włączyć logowanie. Zapisuje odpowiedzi w pliku `.env`, buduje obraz (za pierwszym razem kilka minut, bo pobiera model) i uruchamia kontener. Na koniec podaje adres panelu, np. `http://192.168.1.20:1717`.

Źródło możesz też pominąć w instalatorze i ustawić je później w panelu, w zakładce **Ustawienia**.

### Bez instalatora

```bash
cp .env.example .env      # uzupełnij źródło i port
docker compose up -d --build
```

## Źródła dźwięku

| Rodzaj | Co wpisać |
|---|---|
| Kamera lub rejestrator Dahua | Adres IP, port RTSP (zwykle 554), kanał, strumień (0 główny, 1 pomocniczy), użytkownik i hasło. |
| Dowolny strumień | Pełny adres, np. `rtsp://192.168.1.10:554/stream1` (Hikvision: `/Streaming/Channels/101`, Reolink: `/h264Preview_01_main`). Login i hasło można podać w osobnych polach. |
| Mikrofon (Linux) | Urządzenie ALSA, np. `default` albo `hw:1,0` (lista: `arecord -l`). Kontener musi mieć dostęp do `/dev/snd`, więc instalator dopisuje `COMPOSE_FILE=docker-compose.yml:docker-compose.mic.yml` do `.env`. |

Mikrofon działa tylko w Dockerze na Linuksie. Na macOS i Windows Docker nie widzi mikrofonu, więc trzeba użyć strumienia. Przy mikrofonie nie ma podglądu na żywo, bo mikrofon może mieć naraz tylko jednego słuchacza.

**macOS z Colimą:** jeśli kontener nie łączy się z kamerą w sieci lokalnej, w `extras/macos-rtsp-relay` jest prosty most TCP uruchamiany przez LaunchAgent. W panelu wpisz wtedy adres Maca i port mostu.

## Logowanie i dostęp z internetu

Bez logowania panel jest dostępny dla każdego w sieci lokalnej. Jeśli chcesz otwierać go spoza domu (np. przez reverse proxy jak Caddy, Nginx czy Cloudflare Tunnel), najpierw włącz logowanie w **Ustawienia → Dostęp**.

Panel sam blokuje się, gdy dostanie ruch przez reverse proxy przy wyłączonym logowaniu. Jeśli proxy samo pilnuje logowania, ustaw `ALLOW_PROXY_WITHOUT_LOGIN=1` w `.env`.

Hasło jest zapisywane wyłącznie jako skrót (scrypt). Po 5 nieudanych próbach logowanie z danego adresu jest blokowane na 5 minut.

Zapomniane hasło:

```bash
docker compose exec pies-watch python app.py --disable-login
docker compose restart
```

Nowe hasło bez panelu (podaj je w następnej linii):

```bash
docker compose exec -T pies-watch python app.py --set-login admin
docker compose restart
```

## Ustawienia

Przy pierwszym uruchomieniu ustawienia pochodzą z `.env`. Po pierwszym zapisie w panelu trafiają do `data/settings.json` i to one obowiązują. Zmiany progów, źródła czy strefy działają od razu, bez restartu. Wyjątkiem jest port panelu, który ustawia się w `.env` (`PORT`), a potem uruchamia `docker compose up -d`.

| Ustawienie | Domyślnie | Znaczenie |
|---|---|---|
| Próg modelu | 0,30 | Okna ocenione co najmniej tak wysoko liczą się jako szczekanie. Wyżej = mniej fałszywych wykryć, ale też więcej pominiętego szczekania. |
| Próg „wysoki wynik” | 0,55 | Od tej wartości nagranie dostaje etykietę „Wysoki wynik”. |
| Próg głośności | 0,008 | Poziom RMS (0–1), od którego zaczyna się nasłuch. |
| Przerwa łącząca | 15 s | Krótsza cisza łączy szczeknięcia w jedno nagranie. |
| Margines | 3 s | Dźwięk dodawany przed i po wykrytym szczekaniu. |
| Najkrótsze nagranie | 3 s | Krótsze fragmenty są pomijane. |
| Najdłuższy bufor | 1800 s | Dłuższy, nieprzerwany dźwięk jest analizowany w częściach. |
| Czyszczenie po | 30 dni | Dotyczy plików pomocniczych. Zdarzenia z panelu zostają do archiwizacji. |

Wynik modelu nie jest prawdopodobieństwem, tylko wskazaniem. Nagrania z etykietą „Możliwe szczekanie” warto odsłuchać.

## Dane

Wszystko leży w katalogu `data/` obok `docker-compose.yml`:

- `events.jsonl`: lista zdarzeń, jedna linia JSON na nagranie,
- `szczekanie_RRRRMMDD_GGMMSS.mp3`: nagrania (mono, 64 kb/s),
- `archives/RRRR-MM.zip`: archiwa miesięczne z raportami i nagraniami,
- `settings.json`: ustawienia z panelu (zawiera hasło źródła, ma uprawnienia 600).

Kopia zapasowa to kopia katalogu `data/`.

Archiwum miesiąca powstaje po jego zakończeniu (od 2. dnia kolejnego miesiąca), gdy każde zdarzenie jest potwierdzone albo oznaczone jako fałszywe. Nagrania trafiają wtedy do ZIP-a i znikają z bieżącej listy.

## Aktualizacja

```bash
git pull
docker compose up -d --build
```

## API

| Adres | Opis |
|---|---|
| `GET /api/status` | Stan detektora i statystyki dnia. |
| `GET /api/events` | Bieżące zdarzenia. |
| `GET /api/week-heatmap?day=RRRR-MM-DD` | Dane wykresu tygodniowego. |
| `GET /download/report.csv`, `/download/bark-report.csv` | Raporty CSV (nagłówki w języku panelu). |

Zapytania zmieniające dane muszą mieć nagłówek `X-Requested-With: PiesWatch`.

## Testy

Katalog `tests` nie trafia do obrazu, więc najpierw trzeba go skopiować do kontenera:

```bash
docker compose cp tests pies-watch:/app/tests
docker compose exec pies-watch python -m unittest discover -s tests
```

## Prywatność i prawo

Nagrywanie dźwięku podlega przepisom o ochronie prywatności. Ustaw kamerę tak, żeby obejmowała tylko Twój teren, i nie udostępniaj nagrań z głosami innych osób. Wynik modelu nie jest dowodem. Przed użyciem nagrań w sporze odsłuchaj je i potwierdź ręcznie.

## Licencja

Kod: [MIT](LICENSE). Model YAMNet: Google, Apache 2.0, pobierany podczas budowania obrazu (szczegóły w [NOTICE](NOTICE)).

Pies Watch 1.0 by [@mateopoznan](https://hub.mateopoznan.pl)
