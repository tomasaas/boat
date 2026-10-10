# boat

Raspberry Pi 5 styrer to thrustere (AM32-ESC-er) med **bidirectional DShot600** rett fra GPIO. Styringen er differensiell, fra en web-GUI som Pi-en serverer på ethernet og wifi, og som du åpner i nettleseren på PC-en. Et USB-kamera vises live i GUI-en.

```
make install      # én gang, på Pi-en: bygger libdshot.so, installerer ffmpeg og starter appen ved oppstart
```

Åpne deretter **http://\<pi-hostname\>.local:8000** i nettleseren på PC-en (f.eks. `http://raspberrypi.local:8000`), eller `http://<ip-til-pi>:8000`. Det virker likt over ethernetkabelen og over wifi, fordi appen lytter på alle nettverkskortene. Adressene står også i loggen: `journalctl -u boat -f`.

- **Styring:** hold piltastene eller WASD (begge virker samtidig). Slipp = stopp.
- **Kamera:** øverst på Styring. *Skru av kamera* stopper videoen helt til du skrur den på igjen (huskes etter omstart).
- **Konfigurasjon:** fart, svingstyrke, GPIO og retning per motor, og kameraets oppløsning og fps. Lagres i `config.json`.

Uten `libdshot.so` (f.eks. på Windows) kjører alt i **simulering**: GUI og logikk virker, men ingen signaler sendes.

## Filer

| Fil | Hva |
|---|---|
| `app.py` | **Start denne.** Webserver, GUI og watchdog. |
| `boat.service` | systemd-tjenesten som `make install` legger inn. |
| `index.html` | GUI-en (fanene Styring, Konfigurasjon og Kobling). |
| `boat.py` | Båten: to motorer og differensialstyring (`mix`). |
| `camera.py` | Kameraet: ffmpeg, H.264-video til alle som ser på, og vaktene for CPU og temperatur. |
| `dshot.py` | Én ESC: DShot-frames, telemetri og sendetråd. |
| `dshot_pio.c` | PIO-programmet som lager selve signalet (eneste C-kode). |
| `Makefile` | Henter Raspberry Pi sitt `piolib`, bygger `libdshot.so`, og `make install` legger inn tjenesten. |
| `config.json` | Konfigurasjonen. |

## Oppsett

**Kobling:** se fanen *Kobling* i GUI-en. Den viser pinnene for GPIO-ene i `config.json`. Standard: ESC-signal → GPIO18 (venstre) og GPIO19 (høyre), og GND på ESC → GND på Pi. 3,3 V-signal er nok.

**Pi 5:** Raspberry Pi OS Bookworm, oppdatert (`sudo apt update && sudo apt full-upgrade`). `ls -l /dev/pio0` må finnes. Hvis eieren er `root root`, legg til `SUBSYSTEM=="*-pio", GROUP="gpio", MODE="0660"` i `/etc/udev/rules.d/99-com.rules` og start på nytt.

**AM32 (i AM32-konfiguratoren):** slå på *Bi-Directional* (3D: forover/bakover). Telemetri slås på automatisk, fordi signalet er invertert.

**Appen som tjeneste (systemd):** `make install` legger inn `boat.service`, som starter `app.py` ved oppstart og på nytt hvis den krasjer. Kjør den som din vanlige bruker, ikke med `sudo` (den spør selv om sudo). Tjenesten kjører som den brukeren og fra denne mappa.

| | |
|---|---|
| `journalctl -u boat -f` | Følg loggen. |
| `sudo systemctl restart boat` | Start på nytt (f.eks. etter `git pull`). |
| `sudo systemctl stop boat` | Stopp, f.eks. for å kjøre `python app.py --debug` manuelt. |
| `make uninstall` | Fjern tjenesten. |

**Nettverk:** Appen lytter på port 8000 på alle nettverkskort (ethernet, wifi, IPv4 og IPv6). `<hostname>.local` finnes fordi Raspberry Pi OS kjører avahi (mDNS), og Windows 10/11 forstår `.local`. Bytt navn med `sudo hostnamectl set-hostname boat`, så blir adressen `http://boat.local:8000`.

- *Wifi:* koble Pi-en til samme wifi som PC-en (`sudo nmtui` eller Raspberry Pi Imager). Ingenting annet trengs.
- *Kabel rett mellom PC og Pi:* det er ingen router som deler ut IP-adresser. Hvis `.local`-adressen ikke svarer, la Pi-en dele ut adresser på kabelen: `sudo nmcli con mod "Wired connection 1" ipv4.method shared && sudo nmcli con up "Wired connection 1"`. Da får Pi-en `10.42.0.1`, og PC-en får en adresse fra Pi-en. Ikke bruk dette hvis Pi-ens ethernet senere kobles til en router; sett det tilbake med `ipv4.method auto`.

## Kamera

USB-kameraet kobles i en USB-port på Pi-en. `app.py` finner det selv (`/dev/v4l/by-id/…`), eller sett *Enhet* under Konfigurasjon, f.eks. `/dev/video0`. Brukeren må være i gruppa `video` (sjekk med `groups`; standardbrukeren er det).

Pi 5 har ingen H.264-koder i maskinvare, så `ffmpeg` koder videoen i programvare. Kameraet sender MJPEG i 640×480, 1280×720, 1920×1080 og 2560×1440, med 15 eller 30 bilder/s. Nettleseren spiller H.264-videoen med Media Source Extensions (Chrome, Edge, Firefox). Forsinkelsen er ca. 0,5 s.

**Data over 4G.** Du velger oppløsning og fps, og bitraten følger av dem (`kbps()` i `camera.py`), så bildet holder seg skarpt uten justering. Bitraten er 450 kbit/s ved 480p 15 fps og vokser med piksler per sekund opphøyd i 0,75, fordi store bilder komprimeres bedre per piksel. Er bildet for grøtete eller for dyrt overalt, endrer du `BASE_KBPS`. Konfigurasjon viser GB per time for valget før du lagrer, og Styring viser hva som faktisk går.

| Innstilling | Bitrate | Data per time |
|---|---|---|
| 480p 15 fps (standard) | 450 kbit/s | ~0,20 GB |
| 720p 30 fps | 1725 kbit/s | ~0,78 GB |
| 1080p 30 fps | 3170 kbit/s | ~1,43 GB |
| 1440p 15 fps | 2900 kbit/s | ~1,31 GB |

Det går bare video over nettet når den faktisk vises:
- `ffmpeg` kjører bare når kameraet er på og noen ser på, og stopper 5 s etter at siste seer forsvant.
- Nettleseren stopper videoen når vinduet er minimert eller fanen ikke vises, og når du er på Konfigurasjon eller Kobling.
- *Skru av kamera* stopper den for alle, også andre PC-er.
- GUI-en viser kbit/s og MB brukt i denne økten.

**Motorene går foran videoen.**
- `ffmpeg` kjører med lavest prioritet (`nice 19`) og maks to tråder per ledd. Styringen får alltid CPU.
- Krasjer `ffmpeg`, startes den på nytt etter 2 s. Motorene merker ingenting, fordi den er en egen prosess.
- **Fps-grense per oppløsning:** `PIXEL_BUDGET` i `camera.py` (1920×1080×30 piksler/s) gir maks 15 fps i 1440p og 30 fps ellers. Velger du mer, lagres grensen.
- **Får Pi-en ikke med seg alle bildene og CPU-en er full**, settes oppløsningen ned ett hakk, og GUI-en sier fra. Er CPU-en ikke full, er det kameraet som sender for få bilder (ofte i lite lys), og da vises bare en melding.
- **Over 80 °C** faller videoen tilbake til 480p til Pi-en er under 70 °C. Pi 5 struper selv fra 85 °C. Bruk aktiv kjøler hvis Pi-en sitter i en lukket boks.

**Feilsøking:** `ls /dev/v4l/by-id` viser kameraet. `v4l2-ctl --list-formats-ext` (pakken `v4l-utils`) viser modusene. Uten kamera viser GUI-en ffmpegs testbilde («Testbilde»), og uten `ffmpeg` står det «ffmpeg mangler på Pi-en». Fps, temperatur og advarsler står også i `journalctl -u boat -f`.

## Feilsøking

`python app.py --debug` logger hver throttle-endring. `–` i RPM-feltet betyr at ESC-en ikke svarer, eller at svaret var korrupt.

## Sikkerhet

- Alle på samme nettverk som Pi-en kan åpne GUI-en og kjøre båten. Det er ingen innlogging, så bruk et wifi du stoler på.
- GUI-en sender tastestatus 10 ganger/s. Hvis `app.py` ikke hører noe på 0,5 s, settes begge motorer til 0.
- Når nettleservinduet mister fokus, slippes alle taster.
- Hvis `app.py` dør, stopper DShot-signalet, og AM32 stopper motoren av seg selv.
- Forbindelsene holdes åpne (HTTP/1.1 keep-alive), så de 10 `/drive` i sekundet ikke åpner en ny TCP-forbindelse hver over 4G.

## API-referanse

### `dshot.py`

#### `Esc(gpio, poles=14, reverse=False)`
Én ESC på én GPIO. Starter en bakgrunnstråd som sender ca. 1000 frames/s. De første 0,5 s sendes 0 for å arme ESC-en.

| | |
|---|---|
| `esc.throttle` | `float`, −1..1. 0 = stopp, negativ = revers. Kan settes når som helst. |
| `esc.rpm` | `int` mekanisk RPM fra telemetri, eller `None` hvis siste svar manglet eller var korrupt. |
| `esc.reverse` | `bool`, snur retningen. |
| `esc.close()` | Sender 0 i 0,1 s og frigjør PIO. |

Kaster `RuntimeError` hvis PIO ikke kan åpnes. Koden står i feilmeldingen: −1 betyr at `/dev/pio0` mangler eller at du ikke har tilgang, −2 at det ikke er plass til PIO-programmet, −3 at ingen state machine er ledig (maks 4 ESC-er).

#### Funksjoner
| | |
|---|---|
| `throttle_to_value(t) -> int` | −1..1 → DShot 3D-verdi: 0 = stopp, 48–1047 = revers, 1048–2047 = forover. |
| `make_frame(value) -> int` | 11-bits verdi → 16-bits frame med invertert CRC (ber om telemetri). |
| `decode_erpm(raw) -> int \| None` | Rått 21-bits svar → eRPM. `None` ved manglende eller korrupt svar. |
| `SIMULATED` | `True` hvis `libdshot.so` mangler. |

### `boat.py`

#### `Boat(config)`
| | |
|---|---|
| `boat.drive(surge, yaw)` | `surge`: +1 fram / −1 bak. `yaw`: +1 styrbord / −1 babord. |
| `boat.stop()` | Samme som `drive(0, 0)`. |
| `boat.status() -> dict` | `{"simulated", "left": {"throttle", "rpm"}, "right": {...}}` |
| `boat.left`, `boat.right` | De to `Esc`-objektene. |
| `boat.close()` | Stopper begge. |

#### `mix(surge, yaw, speed, turn) -> (left, right)`
```
left  = surge + turn * yaw
right = surge - turn * yaw
```
Hvis en side blir over 1, skaleres begge ned med samme faktor. Deretter ganges begge med `speed`.

| Taster | Venstre | Høyre | Båten |
|---|---|---|---|
| W | + | + | rett fram |
| S | − | − | rett bak |
| D | + | − | snurrer med klokka på stedet |
| W+D | ++ | + | svinger mot styrbord |
| S+D | − | −− | baugen mot styrbord, i revers |

`yaw` gir alltid samme rotasjonsretning for baugen, både forover og i revers.

#### Konfigurasjon (`config.json`)
| Felt | |
|---|---|
| `speed` | Throttle når en tast holdes, 0..1. |
| `turn` | Hvor mye yaw legges til/trekkes fra per side, 0..1. |
| `poles` | Antall magneter i motoren (for RPM). |
| `left.gpio`, `right.gpio` | GPIO-pinne for hver ESC. |
| `left.reverse`, `right.reverse` | Snu en motor som går feil vei. |
| `camera.on` | `false` = ingen video over nettet. Settes med knappen i GUI-en (`POST /camera`). |
| `camera.height` | 480, 720, 1080 eller 1440. |
| `camera.fps` | 1–30, men maks `max_fps(height)` (15 i 1440p). |
| `camera.device` | Tom = første USB-kamera, ellers f.eks. `/dev/video0`. |

`load_config()`, `save_config(cfg)` og `clean_config(cfg)` (fyller inn standardverdier og retter typer).

### HTTP (`app.py`)
| | |
|---|---|
| `GET /` | GUI. |
| `GET /config` | Konfigurasjon som JSON. |
| `POST /config` | Lagrer og starter motorene på nytt. |
| `POST /drive` | `{"surge": -1..1, "yaw": -1..1}` → `Boat.status()`. Må sendes minst hvert 0,5 s. |
| `GET /camera` | Kamerastatus: `on`, `running`, `viewers`, `width`, `height`, `fps`, `actual_fps`, `kbps` (bitraten nå), `temp`, `degraded` (grunn til nedsatt video), `max_fps`. |
| `POST /camera` | `{"on": true/false}`. Lagres i `config.json`. |
| `GET /video` | Video til nettleseren: H.264 i fragmentert MP4, uendelig. `X-Codec` er codec-strengen til MediaSource. 409 når kameraet er av, 503 uten ffmpeg. |

`python app.py --host :: --port 8000 --debug` (standard `--host ::` = alle nettverkskort; `--host 127.0.0.1` = bare Pi-en selv)

### `dshot_pio.c`
| | |
|---|---|
| `int dshot_open(unsigned gpio)` | Åpner PIO, laster programmet (én gang) og tar en state machine. Returnerer handle, eller < 0 ved feil. |
| `uint32_t dshot_send(int h, uint16_t frame)` | Sender én frame og returnerer rått svar (0 = intet svar innen 100 µs). |
| `void dshot_close(int h)` | Frigjør state machinen. |

PIO-programmet bruker 40 PIO-sykluser per DShot-bit (24 MHz) og leser svaret med 32 sykluser per bit (5/4 hastighet). Bytt `DSHOT_BITRATE` for DShot300/1200.

Protokollen er beskrevet i [DShot and Bidirectional DShot](https://brushlesswhoop.com/dshot-and-bidirectional-dshot/). PIO-API-et er [piolib](https://github.com/raspberrypi/utils/tree/master/piolib).
