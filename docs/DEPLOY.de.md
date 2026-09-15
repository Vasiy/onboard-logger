# Installation und Aktualisierung

[English](DEPLOY.md) · **Deutsch** · [Español](DEPLOY.es.md) · [Français](DEPLOY.fr.md) ·
[Italiano](DEPLOY.it.md) · [Nederlands](DEPLOY.nl.md) · [Български](DEPLOY.bg.md) ·
[Русский](DEPLOY.ru.md)

Wie der Code auf das Board kommt, wie er später ersetzt wird und was zu tun ist, wenn ein Austausch
schiefgeht.

Das Board ist **kein Repository**. Darauf liegt genau das, was zuletzt daraufkopiert wurde — deshalb
endet jeder Weg unten mit einer Prüfung, und deshalb bedeutet eine nicht ausgelieferte Änderung, dass
das Motorrad mit altem Code fährt.

## Die drei Wege

| | wann | braucht | wer führt es aus |
|---|---|---|---|
| `install.sh` | einmalig, auf einem frischen Board | Internet auf dem Board, root | du, auf dem Board über ssh |
| `deploy.sh` | tägliche Entwicklung | Board vom Entwicklungsrechner erreichbar | du, auf dem Entwicklungsrechner |
| Config → System | kein Entwicklungsrechner zur Hand (Garage, Straße) | das Archiv auf dem Telefon | die fahrende Person, im Browser |

Alle drei legen den Code an dieselbe Stelle und enden beim selben Dienst. Der Unterschied ist, wer
die Dateien trägt und was sie prüft.

---

## 1. Erstinstallation (`install.sh`)

Wird **als root, auf dem Board** ausgeführt, aus einem Klon des Repositorys:

```bash
git clone <repo> onboard-logger
cd onboard-logger
sudo ./install.sh
```

Das ist der einzige Schritt, der Internet auf dem Board braucht: Pakete werden installiert und der
Steuergeräte-Flasher wird aus dem Quelltext gebaut.

Was es tut, der Reihe nach:

1. **Pakete** — `hostapd dnsmasq wpasupplicant dhcpcd-base python3-venv python3-pip usbutils
   rfkill iw rsync exfatprogs dosfstools fdisk build-essential git`.
   `fdisk` ist wegen `sfdisk` dabei: Ubuntu trennt es von `util-linux` ab, und ein Board, das
   `wipefs` hat, aber kein `sfdisk`, löscht die Partitionstabelle eines USB-Sticks und kann keine
   neue schreiben.
2. **Kopiert das Repository** nach `/opt/onboard-logger` (ohne `.git`, `.venv`, `__pycache__`).
3. **Python-venv** unter `/opt/onboard-logger/.venv` + `requirements.txt`.
4. **Baut `5am_util`** (`denandz/5am_util`) nach `/opt/onboard-logger/bin/5am_util` — die externe
   Binärdatei, die das Lesen und Schreiben der Firmware aufruft.
5. **Legt die Laufzeitkonfiguration an** unter `/etc/onboard-logger/`: `config.json`, `params.json`,
   `ecu_id.json` — **nur wenn nicht vorhanden** (`[ -f ] || cp`). Siehe *Geschichtete Konfiguration*
   weiter unten.
6. **udev-Regeln** — `/dev/kline`; die USB-Laufzeit-Energieverwaltung **abgeschaltet** für den
   OTG-Root-Hub, das FTDI-Kabel, den WLAN-Stick und den Hub, der beide trägt; dazu eine Regel, die
   den Access Point (Adresse, hostapd, dnsmasq) neu setzt, sobald `wlan0` nach einer
   USB-Neuanmeldung wieder auftaucht. Der Runtime-Suspend des dwc2-Root-Hubs verklemmt dessen
   eigenen Resume-Pfad und lässt den Bus unter den Adaptern durchlaufen — so fallen FTDI-Kabel und
   Stick mitten in der Fahrt gemeinsam aus.
   **NetworkManager** lässt `wlan0` in Ruhe, Funk entsperrt und die Regulierungsdomäne aus
   `wifi.country` gesetzt.
7. **Autostart von hostapd/dnsmasq abgeschaltet** — die App startet sie selbst, sobald die
   Konfiguration erzeugt ist, damit deren eigene Startreihenfolge nicht vorher scheitern kann.
   hostapd bekommt zusätzlich ein Drop-in mit `ConditionPathExists=/sys/class/net/wlan0`: ohne
   Stick wird die Unit übersprungen, statt 30 s auf ein Interface zu warten, das nicht kommt, und
   danach endlos neu zu starten.
8. **systemd-Unit** installiert, aktiviert und gestartet.
9. **Log-Disziplin** — das Journal auf 64 MB begrenzt und die Rückschreibverzögerung des
   Seiten-Caches auf ~5 s verkürzt: das Motorrad trennt den Strom über die Zündung, ein sauberes
   Herunterfahren ist die Ausnahme. `rsyslog` wird eingeengt statt entfernt: seine
   `*.*`-Standardregel schrieb jede Zeile ein zweites Mal auf die Karte, doch die eigenen Ereignisse
   des Boards behalten eine Klartextkopie in `/var/log/onboard-logger.log` — ein Stromausfall kann
   das binäre Journal abschneiden, wo eine Textzeile überlebt.
10. **Das Werksimage wird abgespeckt** — `multi-user.target` als Standard, ModemManager, Bluetooth,
    `NetworkManager-wait-online` und die täglichen apt-Timer abgeschaltet. Zusammen kosten sie rund
    zehn Sekunden jedes Starts, und das Motorrad hat kein Internet für sie.

Danach:

```
Web-UI:  http://192.168.5.1/           (beim Debuggen auch über eth0)
prüfen:  systemctl status onboard-logger
         journalctl -u onboard-logger -f
```

### Wo was landet

| Pfad | was | übersteht ein Update |
|---|---|---|
| `/opt/onboard-logger` | der Code | wird davon ersetzt |
| `/opt/onboard-logger/.venv` | Python-Umgebung | ja — zieht mit um, wird auf dem Motorrad nie neu gebaut |
| `/opt/onboard-logger/bin/5am_util` | Steuergeräte-Flasher | ja — ebenso |
| `/etc/onboard-logger/` | Laufzeitkonfiguration, einmalig angelegt | ja — wird nie angefasst |
| `/root/k-line` | Fahrt-, Diagnose-, Firmware- und Update-Protokolle | ja — außerhalb des Baums |
| `/root/firmware` | Steuergeräte-Abbilder | ja — außerhalb des Baums |
| `/opt/updates/` | hochgeladenes Archiv + Rollback-Skript | Arbeitsbereich |
| `/opt/onboard-logger.old` | der vorherige Baum, für den Rollback aufbewahrt | von einem Update angelegt |

---

## 2. Aktualisieren vom Entwicklungsrechner (`./deploy.sh`)

Der Alltagsweg. **Aus dem Wurzelverzeichnis des Repositorys auf dem Entwicklungsrechner** ausführen,
nicht auf dem Board:

```bash
./deploy.sh                 # testen, stempeln, synchronisieren, neu starten, prüfen
./deploy.sh --no-tests      # Offline-Testsuite überspringen
PY=/pfad/zu/python ./deploy.sh
```

Zugangsdaten stehen **nicht** im Skript: `BOARD_HOST`, `BOARD_USER` und `BOARD_PASS` gehören in eine
`.deploy.env` daneben (in `.gitignore`, Vorlage: `.deploy.env.example`) oder in die Umgebung. Ohne
gesetztes Passwort wird ssh-Schlüsselauthentifizierung verwendet.

Was es tut:

1. Führt die gesamte Offline-Suite aus, dazu `import app.main` und eine Syntaxprüfung von `app.js`.
   Ein Fehlschlag stoppt die Auslieferung — zum Motorrad geht nichts.
2. Stempelt `BUILD` (siehe *Versionierung*).
3. `rsync -a --delete` des **gesamten Repositorys**, nicht nur `app/`: `config/fw_layout.json`,
   `config/fw_catalog.json` und `config/dtc/` werden zur Laufzeit gelesen, und eine Auslieferung nur
   von `app/` hat das Board mehr als einmal auf veralteten Daten sitzen lassen. Ausgenommen sind
   `.git`, `.venv`, `bin`, `__pycache__`, `old_logs`, `CONTEXT.md` — das schützt venv und
   Flasher-Binärdatei zugleich vor `--delete`.
4. Startet `onboard-logger` neu und scheitert, wenn der Dienst nicht wieder `active` wird.
5. **Prüft per Prüfsumme in beide Richtungen** — Arbeitsbaum gegen Board und Board gegen Arbeitsbaum.
   Jeder Unterschied ist ein Fehler, keine Warnung.
6. Gibt eine Zeile `layered config: /etc vs repo` aus, die jede geschichtete Datei nennt, deren Kopie
   in `/etc` von der im Repository abweicht.

Die Daten des Boards — `/root/k-line`, `/root/firmware`, `/etc/onboard-logger` — sind konstruktiv
außer Reichweite: sie liegen alle außerhalb des synchronisierten Verzeichnisses.

### Geschichtete Konfiguration: warum eine Änderung in `config/` wirkungslos bleiben kann

`/etc/onboard-logger` **gewinnt** gegen die Kopie im Repository, und zwar für jede geschichtete Datei
(`params.json`, `ecu_id.json`, `actuators.json`, `status_maps.json`, `profiles.json` sowie
`config.json`, `selected.json`, `presets.json`), und `install.sh` legt sie nur *an*. Eine geänderte
`config/params.json` fährt also im rsync mit, und das Board arbeitet weiter mit seiner eigenen
Kopie — so wurde einmal eine Kanalumbenennung ausgeliefert, ohne wirksam zu werden.

`deploy.sh` meldet die Abweichung; sie anzugleichen ist eine bewusste Handlung, und die Kopie des
Boards kann eine Handänderung enthalten, die es zu erhalten lohnt:

```bash
scp <board>:/etc/onboard-logger/params.json ./params.json.board   # zuerst sichern
scp config/params.json <board>:/etc/onboard-logger/
ssh <board> systemctl restart onboard-logger
```

---

## 3. Aktualisieren vom Telefon (Config → System)

Für ein Board, das vom Laptop aus nicht erreichbar ist. Das Archiv bringt die fahrende Person mit;
alles Prüfen erledigt das Board.

Warum kein „pull aus git“: der normale WLAN-Modus ist der **Access Point, in den sich das Telefon
einbucht**, das Board hat also kein Internet, und eine private Gegenstelle bräuchte ein auf dem Gerät
gespeichertes Token.

### Das Archiv bauen

Auf dem Entwicklungsrechner:

```bash
./release.sh                # führt die Suite aus, schreibt dist/onboard-logger-<version>-<sha>.tar.gz
./release.sh --no-tests
OUT_DIR=/irgendwohin ./release.sh
```

Gepackt werden die **versionierten** Dateien plus ein erzeugtes `BUILD`, damit nichts Lokales
mitfährt — keine `.deploy.env`, keine Protokolle, keine Steuergeräte-Abbilder. GitHubs eigenes
*Download ZIP* geht ebenfalls: den Ordner, den es außen herum legt (`onboard-logger-main/`), erkennt
und entfernt das Board.

Angenommen werden `.tar.gz`, `.tgz`, `.tar`, `.zip` — bis 32 MB (128 MB entpackt, 5000 Einträge).

### Anwenden

Config → **System** → *Archiv .tar.gz / .zip hochladen* → bestätigen. Die Anzeige zeigt die laufende
Version, einen Fortschrittsbalken und, hinter *Protokoll*, was das Board gerade tut.

Die Reihenfolge der Schritte ist der ganze Entwurf — **nichts rührt den laufenden Baum an, bevor der
neue sich bewährt hat**:

1. **Ablehnen, wenn der Moment falsch ist.** Ein laufendes Lesen oder Schreiben der Firmware, ein
   offenes Fahrtenprotokoll oder ein laufender Scan blockieren das Update; ein zweites Update
   ebenfalls. Eine *scharfgeschaltete* Aufzeichnungsabsicht bei ausgeschalteter Zündung blockiert
   nicht — es wird nichts geschrieben, und genau dann aktualisiert man das Board.
2. **Entpacken** nach `/opt/onboard-logger.new`. Jeder Eintrag wird geprüft: absolute Pfade, `..`
   und alles, was keine gewöhnliche Datei und kein Verzeichnis ist (symbolische und harte Links,
   Geräte), werden rundweg abgelehnt — das läuft als root direkt neben `/opt`.
3. **Inhaltsprüfung.** `app/main.py`, `app/static/app.js`, `app/static/i18n.js`,
   `app/static/index.html`, `requirements.txt` und `config/config.default.json` müssen vorhanden
   sein, und `requirements.txt` muss **byteweise identisch** zur installierten Fassung sein: `pip`
   braucht PyPI, das Motorrad hat kein Netz, und eine Abhängigkeit, die sich nicht installieren
   lässt, hinterlässt den Dienst mit einem `ImportError` und totem Port 80.
4. **Testen mit dem eigenen Interpreter des Boards**, gegen den vorbereiteten Baum: zuerst
   `import app.main` (60 s), dann jede `tests/test_*.py` der Reihe nach (15 Minuten für alle).
   `.venv` und `bin` werden dem vorbereiteten Baum dafür als symbolische Links geliehen, damit ein
   Fehlschlag den laufenden Dienst unberührt lässt. Der Balken zählt Testdateien. UI-Tests
   überspringen sich ohne `node` selbst.
5. **Austausch** — nur Umbenennungen, das Fenster, in dem kein Baum an seinem Platz ist, ist zwei
   Systemaufrufe breit. `.venv` und `bin/5am_util` *ziehen* in den neuen Baum um; der alte wird zu
   `/opt/onboard-logger.old`.
6. **Wächter scharf schalten, dann neu starten.** `/opt/updates/rollback.sh` wird **vor** dem
   Neustart mit `systemd-run --on-active=120 --unit=onboard-logger-rollback` eingeplant, und der
   Neustart selbst ist abgekoppelt — ihn direkt auszuführen würde den Prozess töten, der dem Browser
   noch die Antwort schuldet.
7. **Bestätigung.** Der neue Code entfernt beim Start `/run/onboard-logger/update-pending`. Tut er
   das nie — weil er sich nicht importieren lässt oder beim Aufbau stirbt — stellt der Wächter
   `/opt/onboard-logger.old` zurück, holt `.venv` und `bin` dorthin und startet neu. Der
   gescheiterte Baum bleibt als `/opt/onboard-logger.failed` erhalten.

`/etc/onboard-logger` wird zu keinem Zeitpunkt angefasst, Einstellungen, Parameterdefinitionen und
Presets überleben also.

### Was zurückbleibt

Jeder Vorgang schreibt `update-<ts>.log` in den Tagesordner neben den Fahrtenprotokollen: sha256 des
Archivs, jede bestandene Prüfung, die Testergebnisse und der Austausch. Zu lesen unter
**Config → System → Board-Protokolle** (Filter *Update*) oder direkt:

```
GET /api/update/log.txt                      # das letzte
GET /api/update/log.txt?file=<tag>/<name>    # ein bestimmtes
```

### Wenn es schiefgeht

Ein Fehlschlag vor dem Austausch ändert nichts: der vorbereitete Baum wird gelöscht, und die Meldung
sagt, welche Prüfung abgelehnt hat. Die häufigen:

| Meldung | Bedeutung |
|---|---|
| Firmware-Vorgang / Fahrtenprotokoll / Scan | abwarten, bis es fertig ist, dann erneut versuchen |
| kein lesbares Archiv | abgebrochener Download, oder gar kein Archiv |
| Pfade außerhalb des Projekts | als unsicher abgelehnt — nicht „reparieren“, sondern die Herkunft klären |
| `requirements.txt` weicht ab | Abhängigkeiten geändert: dafür braucht es `deploy.sh` und ein `pip install` |
| kein vollständiger Baum | falsches Archiv (Teil-Download oder anderes Projekt) |
| der neue Code lässt sich nicht importieren | das Archiv ist defekt — nichts wurde geändert |
| die Offline-Suite ist fehlgeschlagen | der Code ist defekt — nichts wurde geändert |

Ein Fehlschlag *nach* dem Austausch ist genau der Fall für den Wächter: zwei Minuten warten, und die
vorherige Version kommt von allein zurück. Von Hand über ssh, falls es doch einmal so weit kommt:

```bash
systemctl stop onboard-logger
rm -rf /opt/onboard-logger.failed
mv /opt/onboard-logger /opt/onboard-logger.failed
mv /opt/onboard-logger.old /opt/onboard-logger
mv /opt/onboard-logger.failed/.venv /opt/onboard-logger/.venv   # falls der zurückgeholte keins hat
mv /opt/onboard-logger.failed/bin   /opt/onboard-logger/bin
systemctl start onboard-logger
```

---

## Add-ons

Ein Add-on ist eine Seite, die das Board ausliefert, und ein Speicher, den es für diese Seite
führt. **Kein Code eines Add-ons läuft jemals auf dem Board** — es ist reiner Inhalt, und nur
deshalb lässt es sich überhaupt vom Telefon aus installieren. Das eine, das es gibt, ist der
Kennfeld-Viewer: `./release-addon.sh` in `ecu-map-viewer` baut
`ecu-map-viewer-addon-<version>-<sha>.tar.gz`, und **Config → System → Add-ons** nimmt es an.

Alles, was einem Add-on gehört, liegt in einem Verzeichnis:

```
/opt/onboard-logger/addons/maps/addon.json   Name, Version, Titel
/opt/onboard-logger/addons/maps/web/         die Seite, ausgeliefert unter /addons/maps/
/opt/onboard-logger/addons/maps/data/        Dateien des Add-ons, nur über die API erreichbar
```

Die Trennung ist Absicht. Lägen gespeicherte Dateien im ausgelieferten Baum, wären sie auch als
statischer Inhalt erreichbar, typisiert nach ihrer Endung — und eine hochgeladene `.html` liefe
dann im Origin dieses Boards. Ausgelieferte und gespeicherte Dateien sind verschiedene Türen.

`addons/` ist das dritte, was in `/opt/onboard-logger` liegt und aus keinem Release stammt; die
anderen beiden sind `.venv` und `bin/5am_util`. Deshalb schließt `deploy.sh` es aus (was es auch
vor `--delete` schützt), ein Update trägt es über den Tausch und das Rollback bringt es zurück.
Ein Deploy vom Dev-Host rührt ein installiertes Add-on und seine Dateien nie an.

**Entfernen ist `rm -rf` dieses einen Verzeichnisses**: Code und Daten gehen zusammen, sonst
bleibt nichts davon übrig. Der Knopf in Config → System tut genau das und fragt vorher.

Mit installiertem Kennfeld-Viewer bekommt der Firmware-Reiter einen Knopf **Kennfeld zeigen**
neben *Diff 2 .bin*: ein oder mehrere Abbilder anhaken, drücken, und sie öffnen sich im Viewer.
Er benutzt den bereits geöffneten Tab weiter, sodass ein Vergleich nach dem anderen keine Spur
von Fenstern hinterlässt.

Seine XDF-Definitionen legt der Viewer im eigenen Speicher des Add-ons ab — der Logger erfährt nie,
was eine `.xdf` ist. Einmal hineingezogen, bleibt sie auf dem Board, und ein späteres *Kennfeld
zeigen* zeichnet sofort.

---

## Versionierung

- **`VERSION`** ist versioniert und wird von Hand erhöht — die Ausgabenummer, derzeit `0.2.3`.
- **`BUILD`** liegt daneben, enthält `<version> <describe> <branch> <datum>`, wird von `deploy.sh`
  wie von `release.sh` gestempelt und **nie eingecheckt**: eine Datei, die das Board in seinem
  eigenen Baum erzeugt hat, würde die Prüfsummenkontrolle von `deploy.sh` in beide Richtungen
  zerbrechen.

Config → System zeigt `BUILD`, wenn der Baum gestempelt wurde, sonst die reine Ausgabenummer. Ein
Board, das nie gestempelt wurde, sagt das, statt eine leere Zeile zu zeigen.

Eine Ausgabe schneiden: `VERSION` ändern, einchecken, dann `./release.sh` (oder `./deploy.sh`).

---

## Welchen Weg soll ich nehmen?

- **Code während der Entwicklung ändern** → `./deploy.sh`. Nur dieser Weg prüft, dass das Board dem
  Arbeitsbaum Byte für Byte entspricht.
- **Eine Python-Abhängigkeit hinzufügen oder anheben** → `./deploy.sh` und danach `pip install` auf
  dem Board. Die Update-Schaltfläche lehnt eine geänderte `requirements.txt` mit Absicht ab.
- **Weg vom Entwicklungsrechner** → vorher `./release.sh`, das Archiv auf dem Telefon behalten und in
  Config → System hochladen.
- **Ein Board, das die App gar nicht mehr startet** → ssh und die Handwiederherstellung oben; ist der
  Baum selbst weg, noch einmal `install.sh` (es bewahrt `/etc/onboard-logger` und die Protokolle).
