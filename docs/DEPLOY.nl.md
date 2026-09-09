# Installatie en bijwerken

[English](DEPLOY.md) · [Deutsch](DEPLOY.de.md) · [Español](DEPLOY.es.md) ·
[Français](DEPLOY.fr.md) · [Italiano](DEPLOY.it.md) · **Nederlands** ·
[Български](DEPLOY.bg.md) · [Русский](DEPLOY.ru.md)

Hoe de code op het board komt, hoe die daarna wordt vervangen, en wat te doen als een vervanging
misgaat.

Het board is **geen repository**. Erop staat precies wat er als laatste naartoe is gekopieerd —
daarom eindigt elk pad hieronder in een controle, en daarom betekent een niet-uitgerolde wijziging
dat de motor op oude code rijdt.

## De drie wegen

| | wanneer | vereist | wie voert het uit |
|---|---|---|---|
| `install.sh` | eenmalig, op een vers board | internet op het board, root | jij, op het board via ssh |
| `deploy.sh` | dagelijkse ontwikkeling | board bereikbaar vanaf de ontwikkelmachine | jij, op de ontwikkelmachine |
| Config → System | geen ontwikkelmachine bij de hand (garage, weg) | het archief op de telefoon | de rijder, in de browser |

Alle drie zetten de code op dezelfde plek en eindigen bij dezelfde service. Het verschil is wie de
bestanden draagt en wat ze controleert.

---

## 1. Eerste installatie (`install.sh`)

Draai dit **als root, op het board**, vanuit een kloon van de repository:

```bash
git clone <repo> onboard-logger
cd onboard-logger
sudo ./install.sh
```

Dit is de enige stap die internet op het board nodig heeft: er worden pakketten geïnstalleerd en de
ECU-flasher wordt uit de broncode gebouwd.

Wat het doet, op volgorde:

1. **Pakketten** — `hostapd dnsmasq wpasupplicant dhcpcd-base python3-venv python3-pip usbutils
   rfkill iw rsync exfatprogs dosfstools fdisk build-essential git`.
   `fdisk` zit erbij om `sfdisk`: Ubuntu haalt dat uit `util-linux`, en een board dat wel `wipefs`
   heeft maar geen `sfdisk` wist de partitietabel van een USB-stick en kan er geen nieuwe schrijven.
2. **Kopieert de repository** naar `/opt/onboard-logger` (zonder `.git`, `.venv`, `__pycache__`).
3. **Python-venv** in `/opt/onboard-logger/.venv` + `requirements.txt`.
4. **Bouwt `5am_util`** (`denandz/5am_util`) naar `/opt/onboard-logger/bin/5am_util` — het externe
   programma dat het lezen en schrijven van firmware aanroept.
5. **Zaait de runtimeconfiguratie** in `/etc/onboard-logger/`: `config.json`, `params.json`,
   `ecu_id.json` — **alleen als ze ontbreken** (`[ -f ] || cp`). Zie *Gelaagde configuratie*
   hieronder.
6. **udev-regels** — `/dev/kline`; USB-runtimeenergiebeheer **uitgezet** voor de OTG-roothub, de
   FTDI-kabel, de wifi-dongle en de hub die ze draagt; plus een regel die het accesspoint (adres,
   hostapd, dnsmasq) opnieuw toepast zodra `wlan0` na een USB-herenumeratie weer verschijnt.
   Runtime-suspend van de dwc2-roothub loopt vast in zijn eigen resumepad en laat de bus onder de
   adapters doorcyclen — zo vallen een FTDI-kabel en een dongle tegelijk weg tijdens een rit.
   **NetworkManager** krijgt te horen `wlan0` met rust te laten, de radio wordt vrijgegeven en het
   regelgevingsdomein uit `wifi.country` gezet.
7. **Autostart van hostapd/dnsmasq uitgezet** — de app zet ze zelf aan zodra de configuratie er is,
   zodat hun eigen opstartvolgorde niet eerder kan falen. hostapd krijgt daarnaast een drop-in met
   `ConditionPathExists=/sys/class/net/wlan0`: zonder dongle wordt de unit overgeslagen in plaats
   van 30 s te wachten op een interface die niet komt en daarna eindeloos te herstarten.
8. **systemd-unit** geïnstalleerd, ingeschakeld en gestart.
9. **Logdiscipline** — het journal begrensd op 64 MB en de terugschrijfvertraging van de paginacache
   teruggebracht tot ~5 s: de motor haalt de stroom eraf met het contact, een nette afsluiting is de
   uitzondering. `rsyslog` wordt versmald in plaats van verwijderd: zijn standaardregel `*.*` schreef
   elke regel een tweede keer naar de kaart, maar de eigen gebeurtenissen van het board houden een
   kopie in platte tekst in `/var/log/onboard-logger.log` — een stroomonderbreking kan het binaire
   journal afkappen waar een tekstregel het overleeft.
10. **De fabrieksimage wordt uitgedund** — `multi-user.target` als standaard, en ModemManager,
    Bluetooth, `NetworkManager-wait-online` en apt's dagelijkse timers uitgezet. Samen kosten ze
    zo'n tien seconden van elke start, en de motor heeft geen internet voor ze.

Daarna:

```
Web-UI:       http://192.168.5.1/        (tijdens debuggen ook via eth0)
controleren:  systemctl status onboard-logger
              journalctl -u onboard-logger -f
```

### Waar alles terechtkomt

| pad | wat | overleeft een update |
|---|---|---|
| `/opt/onboard-logger` | de code | wordt erdoor vervangen |
| `/opt/onboard-logger/.venv` | Python-omgeving | ja — verhuist mee, wordt op de motor nooit herbouwd |
| `/opt/onboard-logger/bin/5am_util` | ECU-flasher | ja — idem |
| `/etc/onboard-logger/` | levende configuratie, eenmalig gezaaid | ja — wordt nooit aangeraakt |
| `/root/k-line` | rit-, diagnose-, firmware- en updatelogs | ja — buiten de boom |
| `/root/firmware` | ECU-images | ja — buiten de boom |
| `/opt/updates/` | geüpload archief + het terugrolscript | werkgebied |
| `/opt/onboard-logger.old` | de vorige boom, bewaard om terug te rollen | wordt door een update gemaakt |

---

## 2. Bijwerken vanaf de ontwikkelmachine (`./deploy.sh`)

De dagelijkse weg. Draai het **vanuit de hoofdmap van de repository op de ontwikkelmachine**, niet op
het board:

```bash
./deploy.sh                 # testen, stempelen, synchroniseren, herstarten, verifiëren
./deploy.sh --no-tests      # de offline testsuite overslaan
PY=/pad/naar/python ./deploy.sh
```

Inloggegevens staan **niet** in het script: `BOARD_HOST`, `BOARD_USER` en `BOARD_PASS` horen in een
`.deploy.env` ernaast (door git genegeerd, sjabloon in `.deploy.env.example`) of in de omgeving.
Zonder wachtwoord wordt ssh-sleutelauthenticatie gebruikt.

Wat het doet:

1. Draait de hele offline suite, `import app.main` en een syntaxiscontrole van `app.js`. Een fout
   stopt de uitrol — er gaat niets naar de motor.
2. Stempelt `BUILD` (zie *Versies*).
3. `rsync -a --delete` van de **hele repository**, niet alleen `app/`: `config/fw_layout.json`,
   `config/fw_catalog.json` en `config/dtc/` worden tijdens het draaien gelezen, en een uitrol van
   alleen `app/` liet het board meer dan eens op verouderde gegevens staan. Uitgesloten zijn `.git`,
   `.venv`, `bin`, `__pycache__`, `old_logs`, `CONTEXT.md` — wat meteen de venv en het
   flasher-programma tegen `--delete` beschermt.
4. Herstart `onboard-logger` en faalt als de service niet weer `active` wordt.
5. **Verifieert per controlesom in beide richtingen** — de werkboom tegen het board en het board
   tegen de werkboom. Elk verschil is een fout, geen waarschuwing.
6. Drukt een regel `layered config: /etc vs repo` af die elke gelaagde bron noemt waarvan de kopie in
   `/etc` afwijkt van die in de repository.

De gegevens van het board — `/root/k-line`, `/root/firmware`, `/etc/onboard-logger` — liggen door de
opzet buiten bereik: ze staan allemaal buiten de gesynchroniseerde map.

### Gelaagde configuratie: waarom een bewerking in `config/` geen effect kan hebben

`/etc/onboard-logger` **wint** van de kopie in de repository, voor elke gelaagde bron
(`params.json`, `ecu_id.json`, `actuators.json`, `status_maps.json`, `profiles.json`, plus
`config.json`, `selected.json`, `presets.json`), en `install.sh` *zaait* ze alleen maar. Een
gewijzigde `config/params.json` reist dus mee in de rsync terwijl het board met zijn eigen kopie
doorgaat — zo ging ooit een kanaalhernoeming de deur uit zonder van kracht te worden.

`deploy.sh` meldt het verschil; het gelijktrekken is een bewuste handeling, en de kopie van het board
kan een handmatige aanpassing bevatten die het bewaren waard is:

```bash
scp <board>:/etc/onboard-logger/params.json ./params.json.board   # eerst een reservekopie
scp config/params.json <board>:/etc/onboard-logger/
ssh <board> systemctl restart onboard-logger
```

---

## 3. Bijwerken vanaf de telefoon (Config → System)

Voor een board dat je niet vanaf een laptop bereikt. Het archief wordt door de rijder meegebracht;
alle controle doet het board.

Waarom geen "pull uit git": de normale Wi-Fi-modus is het **toegangspunt waarop de telefoon
inlogt**, het board heeft dus geen internet, en een privéremote zou een token op het apparaat
vereisen.

### Het archief bouwen

Op de ontwikkelmachine:

```bash
./release.sh                # draait de suite, schrijft dist/onboard-logger-<versie>-<sha>.tar.gz
./release.sh --no-tests
OUT_DIR=/ergens ./release.sh
```

Het pakt de **gevolgde** bestanden plus een gegenereerde `BUILD`, zodat er niets lokaals meereist:
geen `.deploy.env`, geen logs, geen ECU-images. GitHubs eigen *Download ZIP* kan ook: de omhullende
map die het toevoegt (`onboard-logger-main/`) herkent en verwijdert het board.

Geaccepteerd worden `.tar.gz`, `.tgz`, `.tar`, `.zip` — tot 32 MB (128 MB uitgepakt, 5000 items).

### Toepassen

Config → **System** → *Upload .tar.gz / .zip* → bevestigen. Het paneel toont de draaiende versie, een
voortgangsbalk en, achter *Logboek*, wat het board aan het doen is.

De volgorde van de stappen ís het ontwerp — **niets raakt de draaiende boom aan totdat de nieuwe
zich heeft bewezen**:

1. **Weigeren als het moment slecht is.** Een lopende firmwarelezing of -schrijving, een open ritlog
   of een lopende scan blokkeren de update; een tweede update ook. Een *scherpgezette*
   opnamebedoeling met het contact uit blokkeert niet — er wordt niets geschreven, en dat is precies
   het moment waarop iemand het board bijwerkt.
2. **Uitpakken** naar `/opt/onboard-logger.new`. Elk item wordt gecontroleerd: absolute paden, `..`
   en alles wat geen gewoon bestand of map is (symbolische koppelingen, harde koppelingen, apparaten)
   worden botweg geweigerd — dit draait als root pal naast `/opt`.
3. **Inhoudscontrole.** `app/main.py`, `app/static/app.js`, `app/static/i18n.js`,
   `app/static/index.html`, `requirements.txt` en `config/config.default.json` moeten aanwezig zijn,
   en `requirements.txt` moet **byte voor byte identiek** zijn aan de geïnstalleerde: `pip` heeft
   PyPI nodig, de motor heeft geen netwerk, en een afhankelijkheid die niet te installeren is laat de
   service met een `ImportError` en poort 80 dood achter.
4. **Testen met de eigen interpreter van het board**, tegen de klaargezette boom: eerst
   `import app.main` (60 s), daarna elke `tests/test_*.py` op zijn beurt (15 minuten voor het geheel).
   `.venv` en `bin` worden daarvoor als symbolische koppelingen aan de klaargezette boom uitgeleend,
   zodat een mislukking de draaiende service ongemoeid laat. De balk telt testbestanden. UI-tests
   slaan zichzelf over zonder `node`.
5. **Wisselen** — alleen hernoemingen, dus het venster waarin er geen boom op zijn plek staat is twee
   systeemaanroepen breed. `.venv` en `bin/5am_util` *verhuizen* naar de nieuwe boom; de oude wordt
   `/opt/onboard-logger.old`.
6. **De waakhond scherpzetten, dan herstarten.** `/opt/updates/rollback.sh` wordt met
   `systemd-run --on-active=120 --unit=onboard-logger-rollback` ingepland **vóór** de herstart, en de
   herstart zelf is losgekoppeld: hem ter plekke doen zou het proces doden dat de browser nog een
   antwoord schuldig is.
7. **Bevestiging.** De nieuwe code wist bij het opstarten `/run/onboard-logger/update-pending`. Doet
   hij dat nooit — hij importeert niet, of sterft tijdens de opbouw — dan zet de waakhond
   `/opt/onboard-logger.old` terug, haalt `.venv` en `bin` daarheen en herstart. De mislukte boom
   blijft als `/opt/onboard-logger.failed` staan.

`/etc/onboard-logger` wordt op geen enkel moment aangeraakt, dus instellingen, parameterdefinities en
presets overleven.

### Wat het achterlaat

Elke bewerking schrijft `update-<ts>.log` in de dagmap naast de ritlogs: de sha256 van het archief,
elke doorstane controle, de testresultaten en de wissel. Lezen kan in
**Config → System → Boardlogs** (filter *Update*) of rechtstreeks:

```
GET /api/update/log.txt                      # de laatste
GET /api/update/log.txt?file=<dag>/<naam>    # een bepaalde
```

### Als het misgaat

Een mislukking vóór de wissel verandert niets: de klaargezette boom wordt verwijderd en het bericht
zegt welke controle weigerde. De gebruikelijke:

| melding | betekenis |
|---|---|
| firmwarebewerking / ritlog / scan | wachten tot het klaar is en opnieuw proberen |
| geen leesbaar archief | afgebroken download, of helemaal geen archief |
| paden buiten het project | geweigerd als onveilig — niet "repareren", maar uitzoeken waar het vandaan komt |
| `requirements.txt` wijkt af | afhankelijkheden veranderd: hiervoor is `deploy.sh` en een `pip install` nodig |
| geen volledige boom | verkeerd archief (deeldownload of een ander project) |
| de nieuwe code importeert niet | het archief is stuk — er is niets gewijzigd |
| de offline suite faalde | de code is stuk — er is niets gewijzigd |

Een mislukking *na* de wissel is precies waar de waakhond voor is: twee minuten wachten en de vorige
versie komt vanzelf terug. Met de hand, via ssh, mocht het er ooit van komen:

```bash
systemctl stop onboard-logger
rm -rf /opt/onboard-logger.failed
mv /opt/onboard-logger /opt/onboard-logger.failed
mv /opt/onboard-logger.old /opt/onboard-logger
mv /opt/onboard-logger.failed/.venv /opt/onboard-logger/.venv   # als de teruggezette er geen heeft
mv /opt/onboard-logger.failed/bin   /opt/onboard-logger/bin
systemctl start onboard-logger
```

---

## Add-ons

Een add-on is een pagina die het board serveert en een opslag die het voor die pagina bijhoudt.
**Geen enkele regel code van een add-on draait op het board** — het is alleen inhoud, en juist
daarom is het vanaf een telefoon te installeren. De enige die bestaat is de mapviewer:
`./release-addon.sh` in `ecu-map-viewer` bouwt `ecu-map-viewer-addon-<versie>-<sha>.tar.gz`, en
**Config → System → Add-ons** neemt hem aan.

Alles wat een add-on bezit staat in één map:

```
/opt/onboard-logger/addons/maps/addon.json   naam, versie, titel
/opt/onboard-logger/addons/maps/web/         de pagina, geserveerd op /addons/maps/
/opt/onboard-logger/addons/maps/data/        bestanden van de add-on, alleen via de API
```

De scheiding is opzet. Zouden bewaarde bestanden in de geserveerde boom staan, dan waren ze ook als
statische inhoud bereikbaar, getypeerd op hun extensie — en een geüploade `.html` zou dan in de
origin van dit board draaien. Serveren en bewaren zijn verschillende deuren.

`addons/` is het derde dat in `/opt/onboard-logger` staat zonder uit een release te komen; de andere
twee zijn `.venv` en `bin/5am_util`. Daarom sluit `deploy.sh` het uit (wat het meteen tegen
`--delete` beschermt), draagt een update het over de wissel heen en haalt de rollback het terug. Een
deploy vanaf de dev-machine raakt een geïnstalleerde add-on en zijn bestanden nooit aan.

**Verwijderen is `rm -rf` van die ene map**: code en data gaan samen, en er blijft nergens iets van
over. De knop in Config → System doet precies dat, en vraagt eerst.

Met de mapviewer geïnstalleerd krijgt het tabblad Firmware een knop **Map tonen** naast *Diff 2
.bin*: vink een of meer images aan, druk erop, en ze openen in de viewer. Hij hergebruikt het
tabblad dat hij al opende, zodat de ene kalibratie na de andere vergelijken geen spoor van vensters
achterlaat.

Zijn XDF-definities bewaart de viewer in de eigen opslag van de add-on — het board leert nooit wat
een `.xdf` is. Sleep er eenmaal een in en hij blijft op het board, zodat een latere *Map tonen*
meteen tekent.

---

## Versies

- **`VERSION`** wordt door git gevolgd en met de hand opgehoogd — het uitgavenummer, nu `0.2.0`.
- **`BUILD`** staat ernaast, bevat `<versie> <describe> <tak> <datum>`, wordt door zowel `deploy.sh`
  als `release.sh` gestempeld en wordt **nooit gecommit**: een bestand dat het board in zijn eigen
  boom had gemaakt zou de controlesomverificatie van `deploy.sh` in beide richtingen breken.

Config → System toont `BUILD` als de boom gestempeld is, en anders het kale uitgavenummer. Een board
dat nooit gestempeld is zegt dat, in plaats van een lege regel te tonen.

Een uitgave maken: `VERSION` aanpassen, committen, dan `./release.sh` (of `./deploy.sh`).

---

## Welke moet ik gebruiken?

- **Code wijzigen tijdens ontwikkeling** → `./deploy.sh`. Het is het enige pad dat verifieert dat het
  board byte voor byte met de werkboom overeenkomt.
- **Een Python-afhankelijkheid toevoegen of ophogen** → `./deploy.sh` en daarna `pip install` op het
  board. De updateknop weigert een gewijzigde `requirements.txt` met opzet.
- **Weg van de ontwikkelmachine** → vooraf `./release.sh`, het archief op de telefoon houden, het in
  Config → System uploaden.
- **Een board dat de app helemaal niet meer start** → ssh en het handmatige herstel hierboven; is de
  boom zelf weg, dan opnieuw `install.sh` (het behoudt `/etc/onboard-logger` en de logs).
