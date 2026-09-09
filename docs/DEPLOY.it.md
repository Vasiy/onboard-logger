# Installazione e aggiornamento

[English](DEPLOY.md) · [Deutsch](DEPLOY.de.md) · [Español](DEPLOY.es.md) ·
[Français](DEPLOY.fr.md) · **Italiano** · [Nederlands](DEPLOY.nl.md) ·
[Български](DEPLOY.bg.md) · [Русский](DEPLOY.ru.md)

Come il codice arriva sulla scheda, come viene sostituito in seguito e cosa fare quando una
sostituzione va male.

La scheda **non è un repository**. Contiene esattamente ciò che vi è stato copiato l'ultima volta:
per questo ogni percorso qui sotto termina con una verifica, e per questo una modifica non
distribuita significa che la moto va con codice vecchio.

## Le tre strade

| | quando | serve | chi lo esegue |
|---|---|---|---|
| `install.sh` | una volta, su una scheda nuova | internet sulla scheda, root | tu, sulla scheda via ssh |
| `deploy.sh` | sviluppo quotidiano | scheda raggiungibile dalla macchina di sviluppo | tu, sulla macchina di sviluppo |
| Config → System | nessuna macchina di sviluppo a portata (garage, strada) | l'archivio sul telefono | chi guida, dal browser |

Tutte e tre depositano il codice nello stesso posto e finiscono nello stesso servizio. La differenza
sta in chi porta i file e in che cosa li controlla.

---

## 1. Prima installazione (`install.sh`)

Si esegue **come root, sulla scheda**, da un clone del repository:

```bash
git clone <repo> onboard-logger
cd onboard-logger
sudo ./install.sh
```

È l'unico passo che richiede internet sulla scheda: installa i pacchetti e compila dai sorgenti il
programmatore della centralina.

Cosa fa, nell'ordine:

1. **Pacchetti** — `hostapd dnsmasq wpasupplicant dhcpcd-base python3-venv python3-pip usbutils
   rfkill iw rsync exfatprogs dosfstools fdisk build-essential git`.
   `fdisk` c'è per `sfdisk`: Ubuntu lo separa da `util-linux`, e una scheda che ha `wipefs` ma non
   `sfdisk` cancella la tabella delle partizioni di una chiavetta e non riesce a scriverne una nuova.
2. **Copia il repository** in `/opt/onboard-logger` (senza `.git`, `.venv`, `__pycache__`).
3. **Ambiente Python** in `/opt/onboard-logger/.venv` + `requirements.txt`.
4. **Compila `5am_util`** (`denandz/5am_util`) in `/opt/onboard-logger/bin/5am_util`: il binario
   esterno a cui si appoggiano la lettura e la scrittura del firmware.
5. **Semina la configurazione di esecuzione** in `/etc/onboard-logger/`: `config.json`,
   `params.json`, `ecu_id.json` — **solo se assenti** (`[ -f ] || cp`). Vedi *Configurazione a
   strati* più avanti.
6. **Regole udev** — `/dev/kline`; la gestione dell'energia USB a runtime **disattivata** per l'hub
   radice OTG, il cavo FTDI, la chiavetta Wi-Fi e l'hub che li porta; più una regola che riapplica
   l'access point (indirizzo, hostapd, dnsmasq) ogni volta che `wlan0` ricompare dopo una
   rienumerazione USB. Il runtime-suspend dell'hub radice dwc2 inceppa il suo stesso percorso di
   ripresa e fa ciclare il bus sotto gli adattatori: è così che un cavo FTDI e una chiavetta cadono
   insieme in mezzo a un giro.
   A **NetworkManager** viene detto di lasciare stare `wlan0`, la radio viene sbloccata e il dominio
   normativo impostato da `wifi.country`.
7. **Avvio automatico di hostapd/dnsmasq disattivato**: l'applicazione li tira su da sé una volta
   generata la configurazione, così il loro ordine di avvio non può fallire prima. hostapd riceve
   inoltre un drop-in con `ConditionPathExists=/sys/class/net/wlan0`: senza chiavetta l'unità viene
   saltata invece di attendere 30 s un'interfaccia che non arriverà e poi riavviarsi all'infinito.
8. **Unità systemd** installata, abilitata e avviata.
9. **Disciplina dei log**: il journal limitato a 64 MB e il ritardo di scrittura differita della
   page cache ridotto a ~5 s: la moto toglie corrente con il quadro, uno spegnimento pulito è
   l'eccezione. `rsyslog` viene ristretto anziché rimosso: la sua regola `*.*` di serie scriveva
   ogni riga una seconda volta sulla scheda, ma gli eventi propri della scheda conservano una copia
   in chiaro in `/var/log/onboard-logger.log`, perché un'interruzione di corrente può troncare il
   journal binario dove una riga di testo sopravvive.
10. **L'immagine di fabbrica viene sfoltita**: `multi-user.target` come predefinito, e ModemManager,
    Bluetooth, `NetworkManager-wait-online` e i timer giornalieri di apt disattivati. Insieme
    costano una decina di secondi a ogni avvio, e la moto non ha internet per loro.

Poi:

```
Interfaccia web:  http://192.168.5.1/    (anche via eth0 in fase di debug)
verifica:         systemctl status onboard-logger
                  journalctl -u onboard-logger -f
```

### Dove finisce ogni cosa

| percorso | cosa | sopravvive a un aggiornamento |
|---|---|---|
| `/opt/onboard-logger` | il codice | è ciò che viene sostituito |
| `/opt/onboard-logger/.venv` | ambiente Python | sì — trasloca, sulla moto non è ricostruibile |
| `/opt/onboard-logger/bin/5am_util` | programmatore della centralina | sì — allo stesso modo |
| `/etc/onboard-logger/` | configurazione viva, seminata una volta | sì — non viene mai toccata |
| `/root/k-line` | log di marcia, diagnostica, firmware e aggiornamenti | sì — fuori dall'albero |
| `/root/firmware` | immagini della centralina | sì — fuori dall'albero |
| `/opt/updates/` | archivio caricato + script di ripristino | area di lavoro |
| `/opt/onboard-logger.old` | l'albero precedente, tenuto per il ripristino | creato da un aggiornamento |

---

## 2. Aggiornare dalla macchina di sviluppo (`./deploy.sh`)

La strada di tutti i giorni. Si esegue **dalla radice del repository sulla macchina di sviluppo**,
non sulla scheda:

```bash
./deploy.sh                 # test, timbro, sincronizzazione, riavvio, verifica
./deploy.sh --no-tests      # salta la suite di test offline
PY=/percorso/a/python ./deploy.sh
```

Le credenziali **non** stanno nello script: `BOARD_HOST`, `BOARD_USER` e `BOARD_PASS` vanno in un
`.deploy.env` accanto (ignorato da git, modello in `.deploy.env.example`) o nell'ambiente. Senza
password si usa l'autenticazione a chiave ssh.

Cosa fa:

1. Esegue l'intera suite offline, `import app.main` e un controllo di sintassi di `app.js`. Un
   fallimento ferma la distribuzione: alla moto non arriva nulla.
2. Timbra `BUILD` (vedi *Versioni*).
3. `rsync -a --delete` dell'**intero repository**, non del solo `app/`: `config/fw_layout.json`,
   `config/fw_catalog.json` e `config/dtc/` vengono letti a runtime, e una distribuzione del solo
   `app/` ha lasciato la scheda su dati vecchi più di una volta. Esclusi `.git`, `.venv`, `bin`,
   `__pycache__`, `old_logs`, `CONTEXT.md`, il che protegge anche l'ambiente virtuale e il binario
   del programmatore da `--delete`.
4. Riavvia `onboard-logger` e fallisce se il servizio non torna `active`.
5. **Verifica per checksum in entrambe le direzioni**: l'albero di lavoro contro la scheda e la
   scheda contro l'albero di lavoro. Qualsiasi differenza è un errore, non un avviso.
6. Stampa una riga `layered config: /etc vs repo` che nomina ogni risorsa a strati la cui copia in
   `/etc` differisce da quella del repository.

I dati della scheda — `/root/k-line`, `/root/firmware`, `/etc/onboard-logger` — sono fuori portata
per costruzione: stanno tutti fuori dalla cartella sincronizzata.

### Configurazione a strati: perché una modifica in `config/` può non avere effetto

`/etc/onboard-logger` **vince** sulla copia del repository per ogni risorsa a strati (`params.json`,
`ecu_id.json`, `actuators.json`, `status_maps.json`, `profiles.json`, oltre a `config.json`,
`selected.json`, `presets.json`), e `install.sh` si limita a *seminarle*. Un `config/params.json`
modificato viaggia quindi nel rsync mentre la scheda continua con la propria copia: è così che una
volta è partita la rinomina di un canale senza entrare in vigore.

`deploy.sh` segnala la divergenza; allinearla è un atto deliberato, e la copia della scheda può
contenere una modifica fatta a mano che vale la pena conservare:

```bash
scp <board>:/etc/onboard-logger/params.json ./params.json.board   # prima il backup
scp config/params.json <board>:/etc/onboard-logger/
ssh <board> systemctl restart onboard-logger
```

---

## 3. Aggiornare dal telefono (Config → System)

Per una scheda che non si raggiunge da un portatile. L'archivio lo porta chi guida; tutta la verifica
la fa la scheda.

Perché non un «pull da git»: la modalità Wi-Fi normale è il **punto di accesso a cui si collega il
telefono**, quindi la scheda non ha internet, e un remoto privato richiederebbe un token conservato
sul dispositivo.

### Costruire l'archivio

Sulla macchina di sviluppo:

```bash
./release.sh                # esegue la suite, scrive dist/onboard-logger-<versione>-<sha>.tar.gz
./release.sh --no-tests
OUT_DIR=/da/qualche/parte ./release.sh
```

Impacchetta i file **tracciati** più un `BUILD` generato, così non viaggia nulla di locale: niente
`.deploy.env`, niente log, nessuna immagine della centralina. Va bene anche il *Download ZIP* di
GitHub: la cartella di involucro che aggiunge (`onboard-logger-main/`) viene riconosciuta e tolta
dalla scheda.

Si accettano `.tar.gz`, `.tgz`, `.tar`, `.zip` — fino a 32 MB (128 MB da scompattati, 5000 voci).

### Applicarlo

Config → **System** → *Carica .tar.gz / .zip* → confermare. Il pannello mostra la versione in
esecuzione, una barra di avanzamento e, dietro *Registro*, cosa sta facendo la scheda.

L'ordine dei passi è tutto il progetto: **niente tocca l'albero in esecuzione finché quello nuovo non
si è dimostrato valido**:

1. **Rifiutare se il momento è sbagliato.** Una lettura o scrittura di firmware in corso, un log di
   marcia aperto o una scansione in corso bloccano l'aggiornamento; anche un secondo aggiornamento.
   Un'intenzione di registrazione *armata* con il quadro spento non blocca: non si scrive nulla, ed è
   esattamente il momento in cui si aggiorna la scheda.
2. **Scompattare** in `/opt/onboard-logger.new`. Ogni voce viene validata: percorsi assoluti, `..` e
   tutto ciò che non è un file normale o una cartella (collegamenti simbolici, collegamenti fisici,
   dispositivi) vengono respinti senz'altro — questo gira da root proprio accanto a `/opt`.
3. **Controllo del contenuto.** `app/main.py`, `app/static/app.js`, `app/static/i18n.js`,
   `app/static/index.html`, `requirements.txt` e `config/config.default.json` devono esserci, e
   `requirements.txt` deve essere **identico byte per byte** a quello installato: `pip` ha bisogno di
   PyPI, la moto non ha rete, e una dipendenza che non si può installare lascia il servizio con un
   `ImportError` e la porta 80 morta.
4. **Provare con l'interprete della scheda stessa**, contro l'albero preparato: prima
   `import app.main` (60 s), poi ogni `tests/test_*.py` a turno (15 minuti in tutto). Per questo
   `.venv` e `bin` vengono prestati all'albero preparato come collegamenti simbolici, così un
   fallimento lascia intatto il servizio in esecuzione. La barra conta i file di test. I test
   dell'interfaccia si saltano da soli senza `node`.
5. **Scambio** — solo rinomine, così la finestra in cui nessun albero è al suo posto è larga due
   chiamate di sistema. `.venv` e `bin/5am_util` *traslocano* nell'albero nuovo; il vecchio diventa
   `/opt/onboard-logger.old`.
6. **Armare la sentinella, poi riavviare.** `/opt/updates/rollback.sh` viene programmato con
   `systemd-run --on-active=120 --unit=onboard-logger-rollback` **prima** del riavvio, e il riavvio
   stesso è staccato: farlo in linea ucciderebbe il processo che deve ancora la risposta al browser.
7. **Conferma.** Il codice nuovo cancella `/run/onboard-logger/update-pending` all'avvio. Se non lo
   fa mai — perché non si importa o muore durante la costruzione — la sentinella rimette al suo posto
   `/opt/onboard-logger.old`, vi riporta `.venv` e `bin` e riavvia. L'albero fallito resta come
   `/opt/onboard-logger.failed`.

`/etc/onboard-logger` non viene toccato in nessun momento, quindi impostazioni, definizioni dei
parametri e preset sopravvivono.

### Cosa lascia dietro di sé

Ogni operazione scrive `update-<ts>.log` nella cartella del giorno, accanto ai log di marcia: lo
sha256 dell'archivio, ogni controllo superato, i risultati dei test e lo scambio. Si legge in
**Config → System → Log della scheda** (filtro *Aggiornamento*) oppure direttamente:

```
GET /api/update/log.txt                       # l'ultimo
GET /api/update/log.txt?file=<giorno>/<nome>  # uno preciso
```

### Se fallisce

Un fallimento prima dello scambio non cambia nulla: l'albero preparato viene cancellato e il
messaggio dice quale controllo ha rifiutato. I casi frequenti:

| messaggio | significato |
|---|---|
| operazione firmware / log di marcia / scansione | aspettare la fine e riprovare |
| archivio non leggibile | scaricamento troncato, o non è un archivio |
| percorsi fuori dal progetto | rifiutato perché non sicuro: non «aggiustarlo», ma capire da dove viene |
| `requirements.txt` diverso | le dipendenze sono cambiate: qui servono `deploy.sh` e un `pip install` |
| albero non completo | archivio sbagliato (scaricamento parziale o altro progetto) |
| il codice nuovo non si importa | l'archivio è rotto — non è stato cambiato nulla |
| la suite offline è fallita | il codice è rotto — non è stato cambiato nulla |

Un fallimento *dopo* lo scambio è proprio ciò per cui esiste la sentinella: due minuti di attesa e la
versione precedente torna da sola. A mano, via ssh, se mai si arrivasse a tanto:

```bash
systemctl stop onboard-logger
rm -rf /opt/onboard-logger.failed
mv /opt/onboard-logger /opt/onboard-logger.failed
mv /opt/onboard-logger.old /opt/onboard-logger
mv /opt/onboard-logger.failed/.venv /opt/onboard-logger/.venv   # se quello ripristinato non ce l'ha
mv /opt/onboard-logger.failed/bin   /opt/onboard-logger/bin
systemctl start onboard-logger
```

---

## Componenti

Un componente è una pagina che la scheda serve e un archivio che tiene per quella pagina. **Nessun
codice di un componente viene mai eseguito sulla scheda**: è solo contenuto, ed è proprio per questo
che si può installare da un telefono. L'unico che esiste è il visualizzatore di mappe:
`./release-addon.sh` in `ecu-map-viewer` costruisce
`ecu-map-viewer-addon-<versione>-<sha>.tar.gz`, e lo accetta **Config → System → Componenti**.

Tutto ciò che appartiene a un componente sta in una sola cartella:

```
/opt/onboard-logger/addons/maps/addon.json   nome, versione, titolo
/opt/onboard-logger/addons/maps/web/         la pagina, servita su /addons/maps/
/opt/onboard-logger/addons/maps/data/        i file del componente, solo tramite l'API
```

La separazione è voluta. Se i file salvati stessero dentro l'albero servito sarebbero raggiungibili
anche come contenuto statico tipizzato dall'estensione, e un `.html` caricato girerebbe nell'origine
della scheda stessa. Servire e conservare sono porte diverse.

`addons/` è la terza cosa che vive dentro `/opt/onboard-logger` senza venire da nessuna release; le
altre due sono `.venv` e `bin/5am_util`. Perciò `deploy.sh` la esclude (il che la protegge anche da
`--delete`), un aggiornamento la porta oltre lo scambio e il ripristino la riporta indietro. Un
deploy dalla macchina di sviluppo non tocca mai un componente installato né i suoi file.

**Rimuoverlo è `rm -rf` di quell'unica cartella**: codice e dati se ne vanno insieme, e altrove non
resta nulla. Il pulsante in Config → System fa esattamente questo, e prima chiede.

Con il visualizzatore installato, la scheda Firmware guadagna un pulsante **Mostra mappa** accanto a
*Diff 2 .bin*: spunta una o più immagini, premi, e si aprono nel visualizzatore. Riusa la scheda già
aperta, così confrontare una calibrazione dopo l'altra non lascia una scia di finestre.

Le sue definizioni XDF il visualizzatore le tiene nell'archivio del componente stesso: la scheda non
impara mai cosa sia un `.xdf`. Lasciacene una dentro una volta e resta sulla scheda, così un *Mostra
mappa* successivo disegna subito.

---

## Versioni

- **`VERSION`** è tracciato e si alza a mano: è il numero di rilascio, ora `0.2.0`.
- **`BUILD`** sta accanto, contiene `<versione> <describe> <ramo> <data>`, viene timbrato sia da
  `deploy.sh` sia da `release.sh` e **non viene mai committato**: un file che la scheda avesse
  generato dentro il proprio albero romperebbe la verifica per checksum di `deploy.sh` in entrambe le
  direzioni.

Config → System mostra `BUILD` quando l'albero è stato timbrato, altrimenti il semplice numero di
rilascio. Una scheda mai timbrata lo dice, invece di mostrare una riga vuota.

Per fare un rilascio: modificare `VERSION`, committarlo, poi `./release.sh` (o `./deploy.sh`).

---

## Quale usare?

- **Modificare codice durante lo sviluppo** → `./deploy.sh`. È l'unica strada che verifica che la
  scheda coincida con l'albero di lavoro byte per byte.
- **Aggiungere o alzare una dipendenza Python** → `./deploy.sh` e poi `pip install` sulla scheda. Il
  pulsante di aggiornamento rifiuta un `requirements.txt` cambiato di proposito.
- **Lontano dalla macchina di sviluppo** → `./release.sh` in anticipo, l'archivio sul telefono,
  caricarlo in Config → System.
- **Una scheda che non avvia proprio l'applicazione** → ssh e il ripristino manuale qui sopra; se
  l'albero stesso è sparito, di nuovo `install.sh` (conserva `/etc/onboard-logger` e i log).
