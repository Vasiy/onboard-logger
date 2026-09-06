# Installation et mise à jour

[English](DEPLOY.md) · [Deutsch](DEPLOY.de.md) · [Español](DEPLOY.es.md) · **Français** ·
[Italiano](DEPLOY.it.md) · [Nederlands](DEPLOY.nl.md) · [Български](DEPLOY.bg.md) ·
[Русский](DEPLOY.ru.md)

Comment le code arrive sur la carte, comment il est remplacé ensuite, et quoi faire quand un
remplacement tourne mal.

La carte n'est **pas un dépôt**. Elle contient exactement ce qui y a été copié la dernière fois —
c'est pourquoi chaque chemin ci-dessous se termine par une vérification, et pourquoi une
modification non déployée signifie que la moto roule avec du vieux code.

## Les trois chemins

| | quand | nécessite | qui l'exécute |
|---|---|---|---|
| `install.sh` | une fois, sur une carte neuve | internet sur la carte, root | toi, sur la carte en ssh |
| `deploy.sh` | développement au quotidien | la carte joignable depuis la machine de dev | toi, sur la machine de dev |
| Config → System | pas de machine de dev sous la main (garage, route) | l'archive sur le téléphone | la personne qui roule, dans le navigateur |

Les trois posent le code au même endroit et aboutissent au même service. La différence tient à qui
porte les fichiers et à ce qui les contrôle.

---

## 1. Première installation (`install.sh`)

À lancer **en root, sur la carte**, depuis un clone du dépôt :

```bash
git clone <repo> onboard-logger
cd onboard-logger
sudo ./install.sh
```

C'est la seule étape qui demande internet sur la carte : elle installe des paquets et compile le
programmeur du calculateur depuis les sources.

Ce qu'elle fait, dans l'ordre :

1. **Paquets** — `hostapd dnsmasq wpasupplicant dhcpcd-base python3-venv python3-pip usbutils
   rfkill iw rsync exfatprogs dosfstools fdisk build-essential git`.
   `fdisk` est là pour `sfdisk` : Ubuntu le sépare de `util-linux`, et une carte qui a `wipefs` mais
   pas `sfdisk` efface la table de partitions d'une clé USB sans pouvoir en écrire une nouvelle.
2. **Copie le dépôt** dans `/opt/onboard-logger` (sans `.git`, `.venv`, `__pycache__`).
3. **Environnement Python** dans `/opt/onboard-logger/.venv` + `requirements.txt`.
4. **Compile `5am_util`** (`denandz/5am_util`) dans `/opt/onboard-logger/bin/5am_util` — le binaire
   externe qu'appellent la lecture et l'écriture du firmware.
5. **Sème la configuration d'exécution** dans `/etc/onboard-logger/` : `config.json`, `params.json`,
   `ecu_id.json` — **seulement si absents** (`[ -f ] || cp`). Voir *Configuration en couches*
   plus bas.
6. **Règle udev** → `/dev/kline`, **NetworkManager** prié de laisser `wlan0` tranquille, radio
   débloquée et domaine réglementaire fixé d'après `wifi.country`.
7. **Démarrage automatique de hostapd/dnsmasq désactivé** — l'application les lève elle-même une fois
   la configuration produite, ainsi leur propre ordre de démarrage ne peut pas échouer avant.
8. **Unité systemd** installée, activée et démarrée.

Ensuite :

```
Interface web :  http://192.168.5.1/     (aussi par eth0 en débogage)
vérifier :       systemctl status onboard-logger
                 journalctl -u onboard-logger -f
```

### Où atterrit quoi

| chemin | quoi | survit à une mise à jour |
|---|---|---|
| `/opt/onboard-logger` | le code | c'est lui qu'on remplace |
| `/opt/onboard-logger/.venv` | environnement Python | oui — déplacé, jamais reconstruit sur la moto |
| `/opt/onboard-logger/bin/5am_util` | programmeur du calculateur | oui — de même |
| `/etc/onboard-logger/` | configuration vive, semée une fois | oui — jamais touchée |
| `/root/k-line` | journaux de trajet, de diagnostic, de firmware et de mise à jour | oui — hors de l'arborescence |
| `/root/firmware` | images du calculateur | oui — hors de l'arborescence |
| `/opt/updates/` | archive envoyée + script de retour arrière | zone de travail |
| `/opt/onboard-logger.old` | l'arborescence précédente, gardée pour le retour arrière | créée par une mise à jour |

---

## 2. Mettre à jour depuis la machine de dev (`./deploy.sh`)

Le chemin de tous les jours. À lancer **depuis la racine du dépôt sur la machine de dev**, pas sur la
carte :

```bash
./deploy.sh                 # tester, estampiller, synchroniser, redémarrer, vérifier
./deploy.sh --no-tests      # sauter la suite de tests hors ligne
PY=/chemin/vers/python ./deploy.sh
```

Les identifiants ne sont **pas** dans le script : `BOARD_HOST`, `BOARD_USER` et `BOARD_PASS` vont
dans un `.deploy.env` à côté (ignoré par git, modèle dans `.deploy.env.example`) ou dans
l'environnement. Sans mot de passe, l'authentification par clé ssh est utilisée.

Ce qu'il fait :

1. Exécute toute la suite hors ligne, `import app.main` et une vérification de syntaxe de `app.js`.
   Un échec arrête le déploiement — rien ne part vers la moto.
2. Estampille `BUILD` (voir *Versionnage*).
3. `rsync -a --delete` de **tout le dépôt**, pas seulement de `app/` : `config/fw_layout.json`,
   `config/fw_catalog.json` et `config/dtc/` sont lus à l'exécution, et un déploiement limité à
   `app/` a laissé la carte sur des données périmées plus d'une fois. Sont exclus `.git`, `.venv`,
   `bin`, `__pycache__`, `old_logs`, `CONTEXT.md` — ce qui protège du même coup l'environnement
   virtuel et le binaire du programmeur contre `--delete`.
4. Redémarre `onboard-logger` et échoue si le service ne revient pas `active`.
5. **Vérifie par somme de contrôle dans les deux sens** — l'arbre de travail contre la carte et la
   carte contre l'arbre de travail. Toute différence est une erreur, pas un avertissement.
6. Affiche une ligne `layered config: /etc vs repo` nommant chaque ressource en couches dont la copie
   dans `/etc` diffère de celle du dépôt.

Les données de la carte — `/root/k-line`, `/root/firmware`, `/etc/onboard-logger` — sont hors
d'atteinte par construction : elles vivent toutes en dehors du répertoire synchronisé.

### Configuration en couches : pourquoi une modification dans `config/` peut rester sans effet

`/etc/onboard-logger` **l'emporte** sur la copie du dépôt pour chaque ressource en couches
(`params.json`, `ecu_id.json`, `actuators.json`, `status_maps.json`, `profiles.json`, ainsi que
`config.json`, `selected.json`, `presets.json`), et `install.sh` ne fait que les *semer*. Un
`config/params.json` modifié voyage donc dans le rsync pendant que la carte continue avec sa propre
copie — c'est ainsi qu'un renommage de canal est parti un jour sans prendre effet.

`deploy.sh` signale l'écart ; l'aligner est un geste délibéré, et la copie de la carte peut contenir
une retouche à la main qui mérite d'être gardée :

```bash
scp <board>:/etc/onboard-logger/params.json ./params.json.board   # sauvegarde d'abord
scp config/params.json <board>:/etc/onboard-logger/
ssh <board> systemctl restart onboard-logger
```

---

## 3. Mettre à jour depuis le téléphone (Config → System)

Pour une carte qu'on ne peut pas joindre depuis un portable. L'archive est apportée par la personne
qui roule ; toute la vérification est faite par la carte.

Pourquoi pas un « pull depuis git » : le mode Wi-Fi normal est le **point d'accès que rejoint le
téléphone**, la carte n'a donc pas d'internet, et un dépôt distant privé exigerait un jeton stocké
sur l'appareil.

### Fabriquer l'archive

Sur la machine de dev :

```bash
./release.sh                # lance la suite, écrit dist/onboard-logger-<version>-<sha>.tar.gz
./release.sh --no-tests
OUT_DIR=/quelque/part ./release.sh
```

Elle empaquette les fichiers **suivis** plus un `BUILD` généré, si bien que rien de local ne part :
pas de `.deploy.env`, pas de journaux, pas d'images du calculateur. Le *Download ZIP* de GitHub
convient aussi : le dossier d'emballage qu'il ajoute (`onboard-logger-main/`) est détecté et retiré
par la carte.

Sont acceptés `.tar.gz`, `.tgz`, `.tar`, `.zip` — jusqu'à 32 Mo (128 Mo décompressés, 5000 entrées).

### L'appliquer

Config → **System** → *Envoyer .tar.gz / .zip* → confirmer. Le panneau montre la version en cours,
une barre de progression et, derrière *Journal*, ce que la carte est en train de faire.

L'ordre des étapes est toute la conception — **rien ne touche l'arborescence en service tant que la
nouvelle n'a pas fait ses preuves** :

1. **Refuser si le moment est mauvais.** Une lecture ou une écriture de firmware en cours, un journal
   de trajet ouvert, un scan en marche : tout cela bloque la mise à jour ; une deuxième mise à jour
   aussi. Une intention d'enregistrement *armée* contact coupé ne bloque pas — rien ne s'écrit, et
   c'est exactement le moment où l'on met la carte à jour.
2. **Décompresser** dans `/opt/onboard-logger.new`. Chaque entrée est validée : chemins absolus,
   `..`, et tout ce qui n'est pas un fichier ordinaire ou un répertoire (liens symboliques, liens
   physiques, périphériques) sont refusés d'emblée — cela tourne en root juste à côté de `/opt`.
3. **Contrôle du contenu.** `app/main.py`, `app/static/app.js`, `app/static/i18n.js`,
   `app/static/index.html`, `requirements.txt` et `config/config.default.json` doivent être présents,
   et `requirements.txt` doit être **identique octet pour octet** à celui installé : `pip` a besoin
   de PyPI, la moto n'a pas de réseau, et une dépendance impossible à installer laisse le service sur
   une `ImportError` et le port 80 mort.
4. **Tester avec l'interpréteur de la carte elle-même**, contre l'arborescence préparée : d'abord
   `import app.main` (60 s), puis chaque `tests/test_*.py` à son tour (15 minutes pour l'ensemble).
   `.venv` et `bin` sont prêtés à l'arborescence préparée sous forme de liens symboliques, de sorte
   qu'un échec laisse le service en service intact. La barre compte les fichiers de test. Les tests
   d'interface se sautent d'eux-mêmes sans `node`.
5. **Échange** — des renommages seulement, la fenêtre pendant laquelle aucune arborescence n'est en
   place fait deux appels système de large. `.venv` et `bin/5am_util` *déménagent* dans la nouvelle ;
   l'ancienne devient `/opt/onboard-logger.old`.
6. **Armer la sentinelle, puis redémarrer.** `/opt/updates/rollback.sh` est programmé avec
   `systemd-run --on-active=120 --unit=onboard-logger-rollback` **avant** le redémarrage, et le
   redémarrage lui-même est détaché : le faire en ligne tuerait le processus qui doit encore sa
   réponse au navigateur.
7. **Confirmation.** Le nouveau code efface `/run/onboard-logger/update-pending` au démarrage. S'il
   ne le fait jamais — il ne s'importe pas, ou meurt pendant la construction — la sentinelle remet
   `/opt/onboard-logger.old` en place, y ramène `.venv` et `bin`, et redémarre. L'arborescence en
   échec est conservée sous `/opt/onboard-logger.failed`.

`/etc/onboard-logger` n'est touché à aucun moment : réglages, définitions de paramètres et préréglages
survivent donc.

### Ce que ça laisse

Chaque opération écrit `update-<ts>.log` dans le dossier du jour, à côté des journaux de trajet : le
sha256 de l'archive, chaque contrôle franchi, les résultats des tests et l'échange. À lire dans
**Config → System → Journaux de la carte** (filtre *Mise à jour*) ou directement :

```
GET /api/update/log.txt                      # le dernier
GET /api/update/log.txt?file=<jour>/<nom>    # un précis
```

### En cas d'échec

Un échec avant l'échange ne change rien : l'arborescence préparée est supprimée et le message dit
quel contrôle a refusé. Les cas courants :

| message | signification |
|---|---|
| opération firmware / journal de trajet / scan | attendre la fin, puis réessayer |
| archive illisible | téléchargement tronqué, ou pas une archive |
| chemins hors du projet | refusée comme dangereuse — ne pas « réparer », mais chercher d'où elle vient |
| `requirements.txt` diffère | les dépendances ont changé : il faut `deploy.sh` et un `pip install` |
| arborescence incomplète | mauvaise archive (téléchargement partiel ou autre projet) |
| le nouveau code ne s'importe pas | l'archive est cassée — rien n'a été changé |
| la suite hors ligne a échoué | le code est cassé — rien n'a été changé |

Un échec *après* l'échange est précisément la raison d'être de la sentinelle : deux minutes d'attente
et la version précédente revient d'elle-même. À la main, en ssh, si l'on en arrive un jour là :

```bash
systemctl stop onboard-logger
rm -rf /opt/onboard-logger.failed
mv /opt/onboard-logger /opt/onboard-logger.failed
mv /opt/onboard-logger.old /opt/onboard-logger
mv /opt/onboard-logger.failed/.venv /opt/onboard-logger/.venv   # si la restaurée n'en a pas
mv /opt/onboard-logger.failed/bin   /opt/onboard-logger/bin
systemctl start onboard-logger
```

---

## Versionnage

- **`VERSION`** est suivi par git et incrémenté à la main — le numéro de version, actuellement
  `0.2.0`.
- **`BUILD`** est à côté, contient `<version> <describe> <branche> <date>`, est estampillé par
  `deploy.sh` comme par `release.sh`, et n'est **jamais commité** : un fichier que la carte aurait
  produit dans sa propre arborescence casserait la vérification par sommes de contrôle de
  `deploy.sh`, dans les deux sens.

Config → System affiche `BUILD` quand l'arborescence a été estampillée, et le simple numéro de
version sinon. Une carte jamais estampillée le dit, au lieu de montrer une ligne vide.

Pour publier une version : modifier `VERSION`, la commiter, puis `./release.sh` (ou `./deploy.sh`).

---

## Lequel choisir ?

- **Modifier du code en développement** → `./deploy.sh`. C'est le seul chemin qui vérifie que la
  carte correspond à l'arbre de travail octet pour octet.
- **Ajouter ou monter une dépendance Python** → `./deploy.sh` puis `pip install` sur la carte. Le
  bouton de mise à jour refuse un `requirements.txt` modifié, et c'est voulu.
- **Loin de la machine de dev** → `./release.sh` à l'avance, garder l'archive sur le téléphone,
  l'envoyer depuis Config → System.
- **Une carte qui ne démarre plus du tout l'application** → ssh et la récupération manuelle
  ci-dessus ; si l'arborescence elle-même a disparu, refaire `install.sh` (il préserve
  `/etc/onboard-logger` et les journaux).
