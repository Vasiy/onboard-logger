# Instalación y actualización

[English](DEPLOY.md) · [Deutsch](DEPLOY.de.md) · **Español** · [Français](DEPLOY.fr.md) ·
[Italiano](DEPLOY.it.md) · [Nederlands](DEPLOY.nl.md) · [Български](DEPLOY.bg.md) ·
[Русский](DEPLOY.ru.md)

Cómo llega el código a la placa, cómo se sustituye después y qué hacer cuando una sustitución sale
mal.

La placa **no es un repositorio**. Contiene exactamente lo que se copió por última vez, y por eso
cada camino de aquí abajo termina en una comprobación, y por eso un cambio sin desplegar significa
que la moto circula con código viejo.

## Los tres caminos

| | cuándo | necesita | quién lo ejecuta |
|---|---|---|---|
| `install.sh` | una vez, en una placa nueva | internet en la placa, root | tú, en la placa por ssh |
| `deploy.sh` | desarrollo del día a día | la placa accesible desde el equipo de desarrollo | tú, en el equipo de desarrollo |
| Config → System | sin equipo de desarrollo a mano (garaje, carretera) | el archivo en el teléfono | quien conduce, en el navegador |

Los tres dejan el código en el mismo sitio y terminan en el mismo servicio. La diferencia es quién
lleva los ficheros y qué los comprueba.

---

## 1. Primera instalación (`install.sh`)

Se ejecuta **como root, en la placa**, desde un clon del repositorio:

```bash
git clone <repo> onboard-logger
cd onboard-logger
sudo ./install.sh
```

Es el único paso que necesita internet en la placa: instala paquetes y compila el grabador de la
centralita desde el código fuente.

Lo que hace, en orden:

1. **Paquetes** — `hostapd dnsmasq wpasupplicant dhcpcd-base python3-venv python3-pip usbutils
   rfkill iw rsync exfatprogs dosfstools fdisk build-essential git`.
   `fdisk` está por `sfdisk`: Ubuntu lo separa de `util-linux`, y una placa que tiene `wipefs` pero
   no `sfdisk` borra la tabla de particiones de un pendrive y no puede escribir una nueva.
2. **Copia el repositorio** a `/opt/onboard-logger` (sin `.git`, `.venv`, `__pycache__`).
3. **Entorno virtual de Python** en `/opt/onboard-logger/.venv` + `requirements.txt`.
4. **Compila `5am_util`** (`denandz/5am_util`) en `/opt/onboard-logger/bin/5am_util`: el binario
   externo al que llaman la lectura y la escritura de firmware.
5. **Siembra la configuración de ejecución** en `/etc/onboard-logger/`: `config.json`, `params.json`,
   `ecu_id.json` — **solo si no están** (`[ -f ] || cp`). Véase *Configuración por capas* más abajo.
6. **Reglas udev** — `/dev/kline`; la gestión de energía USB en ejecución **desactivada** para el
   concentrador raíz OTG, el cable FTDI, el adaptador Wi-Fi y el concentrador que los lleva; y una
   regla que vuelve a aplicar el punto de acceso (dirección, hostapd, dnsmasq) cada vez que `wlan0`
   reaparece tras una reenumeración USB. Suspender el concentrador raíz dwc2 atasca su propia ruta
   de reanudación y hace ciclar el bus bajo los adaptadores: así es como un cable FTDI y un
   adaptador se caen a la vez en plena marcha.
   A **NetworkManager** se le dice que deje en paz `wlan0`, se desbloquea la radio y se fija el
   dominio regulatorio desde `wifi.country`.
7. **Autoarranque de hostapd/dnsmasq desactivado**: la aplicación los levanta ella misma una vez
   generada la configuración, así su propio orden de arranque no puede fallar antes. hostapd recibe
   además un drop-in con `ConditionPathExists=/sys/class/net/wlan0`: sin adaptador la unidad se
   omite, en lugar de esperar 30 s una interfaz que no va a aparecer y luego reiniciarse sin fin.
8. **Unidad systemd** instalada, habilitada y arrancada.
9. **Disciplina de registros**: el diario limitado a 64 MB y el retardo de escritura diferida de la
   caché de páginas reducido a ~5 s: la moto corta la corriente con el encendido, un apagado limpio
   es la excepción. A `rsyslog` se le estrecha en lugar de quitarlo: su regla `*.*` de serie
   escribía cada línea una segunda vez en la tarjeta, pero los eventos propios de la placa conservan
   una copia en texto plano en `/var/log/onboard-logger.log`, porque un corte de corriente puede
   truncar el diario binario donde una línea de texto sobrevive.
10. **La imagen de fábrica se recorta**: `multi-user.target` por defecto, y ModemManager, Bluetooth,
    `NetworkManager-wait-online` y los temporizadores diarios de apt desactivados. Juntos cuestan
    unos diez segundos de cada arranque, y la moto no tiene internet para ellos.

Después:

```
Interfaz web:  http://192.168.5.1/       (también por eth0 mientras se depura)
comprobar:     systemctl status onboard-logger
               journalctl -u onboard-logger -f
```

### Dónde acaba cada cosa

| ruta | qué | sobrevive a una actualización |
|---|---|---|
| `/opt/onboard-logger` | el código | es lo que se sustituye |
| `/opt/onboard-logger/.venv` | entorno de Python | sí — se traslada, nunca se reconstruye en la moto |
| `/opt/onboard-logger/bin/5am_util` | grabador de la centralita | sí — igual |
| `/etc/onboard-logger/` | configuración viva, sembrada una vez | sí — no se toca nunca |
| `/root/k-line` | registros de marcha, diagnóstico, firmware y actualizaciones | sí — fuera del árbol |
| `/root/firmware` | imágenes de la centralita | sí — fuera del árbol |
| `/opt/updates/` | archivo subido + el script de reversión | zona de trabajo |
| `/opt/onboard-logger.old` | el árbol anterior, guardado para revertir | lo crea una actualización |

---

## 2. Actualizar desde el equipo de desarrollo (`./deploy.sh`)

El camino de cada día. Se ejecuta **desde la raíz del repositorio en el equipo de desarrollo**, no en
la placa:

```bash
./deploy.sh                 # probar, sellar, sincronizar, reiniciar, verificar
./deploy.sh --no-tests      # saltarse la batería de pruebas sin conexión
PY=/ruta/a/python ./deploy.sh
```

Las credenciales **no** están en el script: `BOARD_HOST`, `BOARD_USER` y `BOARD_PASS` van en un
`.deploy.env` al lado (ignorado por git, plantilla en `.deploy.env.example`) o en el entorno. Sin
contraseña se usa autenticación por clave ssh.

Lo que hace:

1. Ejecuta toda la batería sin conexión, `import app.main` y una comprobación de sintaxis de
   `app.js`. Un fallo detiene el despliegue: a la moto no llega nada.
2. Sella `BUILD` (véase *Versionado*).
3. `rsync -a --delete` de **todo el repositorio**, no solo de `app/`: `config/fw_layout.json`,
   `config/fw_catalog.json` y `config/dtc/` se leen en ejecución, y un despliegue de solo `app/` dejó
   la placa con datos obsoletos más de una vez. Se excluyen `.git`, `.venv`, `bin`, `__pycache__`,
   `old_logs`, `CONTEXT.md`, lo que además protege el entorno virtual y el binario del grabador
   frente a `--delete`.
4. Reinicia `onboard-logger` y falla si el servicio no vuelve a `active`.
5. **Verifica por suma de comprobación en ambos sentidos**: el árbol de trabajo contra la placa y la
   placa contra el árbol de trabajo. Cualquier diferencia es un error, no un aviso.
6. Imprime una línea `layered config: /etc vs repo` nombrando cada recurso por capas cuya copia en
   `/etc` difiere de la del repositorio.

Los datos de la placa — `/root/k-line`, `/root/firmware`, `/etc/onboard-logger` — quedan fuera de
alcance por construcción: todos viven fuera del directorio sincronizado.

### Configuración por capas: por qué una edición en `config/` puede no surtir efecto

`/etc/onboard-logger` **gana** sobre la copia del repositorio para cada recurso por capas
(`params.json`, `ecu_id.json`, `actuators.json`, `status_maps.json`, `profiles.json`, además de
`config.json`, `selected.json`, `presets.json`), e `install.sh` solo los *siembra*. Así que un
`config/params.json` editado viaja en el rsync y la placa sigue funcionando con su propia copia: así
se envió una vez el renombrado de un canal sin que llegara a aplicarse.

`deploy.sh` informa de la discrepancia; igualarla es un acto deliberado, y la copia de la placa puede
contener una edición a mano que convenga conservar:

```bash
scp <board>:/etc/onboard-logger/params.json ./params.json.board   # primero la copia de seguridad
scp config/params.json <board>:/etc/onboard-logger/
ssh <board> systemctl restart onboard-logger
```

---

## 3. Actualizar desde el teléfono (Config → System)

Para una placa a la que no se llega desde un portátil. El archivo lo lleva quien conduce; toda la
comprobación la hace la placa.

Por qué no un «pull de git»: el modo Wi-Fi normal es el **punto de acceso al que se conecta el
teléfono**, de modo que la placa no tiene internet, y un remoto privado exigiría guardar un token en
el dispositivo.

### Construir el archivo

En el equipo de desarrollo:

```bash
./release.sh                # ejecuta la batería y escribe dist/onboard-logger-<versión>-<sha>.tar.gz
./release.sh --no-tests
OUT_DIR=/algún/sitio ./release.sh
```

Empaqueta los ficheros **versionados** más un `BUILD` generado, así no viaja nada local: ni
`.deploy.env`, ni registros, ni imágenes de la centralita. El *Download ZIP* de GitHub también sirve:
la carpeta envoltorio que añade (`onboard-logger-main/`) la placa la detecta y la quita.

Se aceptan `.tar.gz`, `.tgz`, `.tar`, `.zip` — hasta 32 MB (128 MB descomprimido, 5000 entradas).

### Aplicarlo

Config → **System** → *Subir .tar.gz / .zip* → confirmar. El panel muestra la versión en marcha, una
barra de progreso y, tras *Registro*, lo que la placa está haciendo.

El orden de los pasos es todo el diseño: **nada toca el árbol en marcha hasta que el nuevo se ha
ganado el puesto**:

1. **Rechazar si el momento es malo.** Una lectura o escritura de firmware en curso, un registro de
   marcha abierto o un escaneo en marcha bloquean la actualización; una segunda actualización
   también. Una intención de grabación *armada* con el contacto quitado no bloquea: no se escribe
   nada, y ese es precisamente el momento en que se actualiza la placa.
2. **Descomprimir** en `/opt/onboard-logger.new`. Se valida cada entrada: rutas absolutas, `..` y
   todo lo que no sea un fichero normal o un directorio (enlaces simbólicos, enlaces duros,
   dispositivos) se rechazan sin más — esto corre como root justo al lado de `/opt`.
3. **Comprobación del contenido.** `app/main.py`, `app/static/app.js`, `app/static/i18n.js`,
   `app/static/index.html`, `requirements.txt` y `config/config.default.json` tienen que estar, y
   `requirements.txt` debe ser **idéntico byte a byte** al instalado: `pip` necesita PyPI, la moto no
   tiene red, y una dependencia que no se puede instalar deja el servicio con un `ImportError` y el
   puerto 80 muerto.
4. **Probar con el propio intérprete de la placa**, contra el árbol preparado: primero
   `import app.main` (60 s) y después cada `tests/test_*.py` por turno (15 minutos en total). Para
   eso se prestan `.venv` y `bin` al árbol preparado como enlaces simbólicos, de modo que un fallo
   deja intacto el servicio en marcha. La barra cuenta ficheros de prueba. Las pruebas de interfaz se
   saltan solas sin `node`.
5. **Cambio** — solo renombrados, así la ventana en la que no hay ningún árbol en su sitio tiene el
   ancho de dos llamadas al sistema. `.venv` y `bin/5am_util` se *trasladan* al árbol nuevo; el
   antiguo pasa a ser `/opt/onboard-logger.old`.
6. **Armar el vigilante y luego reiniciar.** `/opt/updates/rollback.sh` se programa con
   `systemd-run --on-active=120 --unit=onboard-logger-rollback` **antes** del reinicio, y el reinicio
   va desacoplado: hacerlo en línea mataría al proceso que aún le debe la respuesta al navegador.
7. **Confirmación.** El código nuevo borra `/run/onboard-logger/update-pending` al arrancar. Si nunca
   lo hace —porque no se importa o muere durante la construcción— el vigilante devuelve
   `/opt/onboard-logger.old` a su sitio, se lleva allí `.venv` y `bin` y reinicia. El árbol que falló
   se conserva como `/opt/onboard-logger.failed`.

`/etc/onboard-logger` no se toca en ningún momento, así que ajustes, definiciones de parámetros y
preajustes sobreviven.

### Qué deja atrás

Cada operación escribe `update-<ts>.log` en la carpeta del día, junto a los registros de marcha: el
sha256 del archivo, cada comprobación superada, los resultados de las pruebas y el cambio. Se lee en
**Config → System → Registros de la placa** (filtro *Actualización*) o directamente:

```
GET /api/update/log.txt                      # el último
GET /api/update/log.txt?file=<día>/<nombre>  # uno concreto
```

### Si falla

Un fallo antes del cambio no altera nada: el árbol preparado se borra y el mensaje dice qué
comprobación lo rechazó. Los habituales:

| mensaje | significado |
|---|---|
| operación de firmware / registro de marcha / escaneo | esperar a que termine y reintentar |
| no es un archivo legible | descarga truncada, o no es un archivo comprimido |
| rutas fuera del proyecto | rechazado por inseguro: no «arreglarlo», sino averiguar de dónde viene |
| `requirements.txt` difiere | han cambiado las dependencias: esto pide `deploy.sh` y un `pip install` |
| no es un árbol completo | archivo equivocado (descarga parcial u otro proyecto) |
| el código nuevo no se importa | el archivo está roto — no se cambió nada |
| la batería sin conexión falló | el código está roto — no se cambió nada |

Un fallo *después* del cambio es justo para lo que está el vigilante: esperar dos minutos y la
versión anterior vuelve sola. A mano, por ssh, si alguna vez se llega a eso:

```bash
systemctl stop onboard-logger
rm -rf /opt/onboard-logger.failed
mv /opt/onboard-logger /opt/onboard-logger.failed
mv /opt/onboard-logger.old /opt/onboard-logger
mv /opt/onboard-logger.failed/.venv /opt/onboard-logger/.venv   # si el restaurado no tiene
mv /opt/onboard-logger.failed/bin   /opt/onboard-logger/bin
systemctl start onboard-logger
```

---

## Complementos

Un complemento es una página que la placa sirve y un almacén que guarda para ella. **Ningún
código de un complemento se ejecuta en la placa** — es solo contenido, y por eso puede instalarse
desde un teléfono. El único que existe es el visor de mapas: `./release-addon.sh` en
`ecu-map-viewer` construye `ecu-map-viewer-addon-<versión>-<sha>.tar.gz`, y lo acepta
**Config → System → Complementos**.

Todo lo que posee un complemento vive en un solo directorio:

```
/opt/onboard-logger/addons/maps/addon.json   nombre, versión, título
/opt/onboard-logger/addons/maps/web/         la página, servida en /addons/maps/
/opt/onboard-logger/addons/maps/data/        archivos del complemento, solo por la API
```

La separación es deliberada. Si los archivos guardados estuvieran dentro del árbol servido también
serían alcanzables como contenido estático tipado por su extensión, y un `.html` subido correría
en el propio origen de esta placa. Servir y guardar son puertas distintas.

`addons/` es lo tercero que vive dentro de `/opt/onboard-logger` sin venir de ninguna versión; los
otros dos son `.venv` y `bin/5am_util`. Por eso `deploy.sh` lo excluye (lo que además lo protege de
`--delete`), una actualización lo pasa al otro lado del intercambio y la reversión lo trae de
vuelta. Un despliegue desde el equipo de desarrollo nunca toca un complemento instalado ni sus
archivos.

**Quitarlo es `rm -rf` de ese único directorio**: código y datos se van juntos y no queda nada en
ninguna otra parte. El botón de Config → System hace exactamente eso, y pregunta antes.

Con el visor instalado, la pestaña Firmware gana un botón **Ver mapa** junto a *Diff 2 .bin*: marca
una o varias imágenes, púlsalo y se abren en el visor. Reutiliza la pestaña que ya abrió, así que
comparar una calibración tras otra no deja un rastro de ventanas.

El visor guarda sus definiciones XDF en el almacén del propio complemento: la placa nunca aprende
qué es un `.xdf`. Suelta una en el visor una vez y se queda en la placa, de modo que un *Ver mapa*
posterior dibuja los mapas de inmediato.

---

## Versionado

- **`VERSION`** está versionado y se sube a mano: el número de publicación, ahora `0.2.4`.
- **`BUILD`** va al lado, contiene `<versión> <describe> <rama> <fecha>`, lo sellan tanto
  `deploy.sh` como `release.sh` y **nunca se confirma**: un fichero que la placa hubiera generado
  dentro de su propio árbol rompería la verificación por sumas de comprobación de `deploy.sh` en
  ambos sentidos.

Config → System muestra `BUILD` cuando el árbol fue sellado y el número de publicación a secas en
caso contrario. Una placa que nunca se selló lo dice, en lugar de mostrar una línea vacía.

Para cortar una publicación: editar `VERSION`, confirmarlo y después `./release.sh` (o
`./deploy.sh`).

---

## ¿Cuál debo usar?

- **Cambiar código durante el desarrollo** → `./deploy.sh`. Es el único camino que verifica que la
  placa coincide con el árbol de trabajo byte a byte.
- **Añadir o subir una dependencia de Python** → `./deploy.sh` y luego `pip install` en la placa. El
  botón de actualización rechaza un `requirements.txt` cambiado a propósito.
- **Lejos del equipo de desarrollo** → `./release.sh` de antemano, el archivo en el teléfono, subirlo
  en Config → System.
- **Una placa que no arranca la aplicación en absoluto** → ssh y la recuperación manual de arriba; si
  el propio árbol ha desaparecido, `install.sh` otra vez (conserva `/etc/onboard-logger` y los
  registros).
