# ESP32 Disk-Temp Fan Controller for TrueNAS

Controllo della velocità di una ventola PWM 4-pin tramite ESP32, pilotato dalla temperatura dei dischi di un server **TrueNAS SCALE**.

Lo script gira in un container Docker su TrueNAS, legge le temperature dei dischi tramite l'API ufficiale e invia all'ESP32 (via USB seriale) la percentuale di velocità calcolata da una curva configurabile. L'ESP32 traduce quella percentuale in un segnale PWM a 25 kHz e legge in tempo reale gli RPM della ventola.

```
┌──────────────┐   API WebSocket    ┌──────────────┐   USB seriale   ┌──────────┐   PWM 25kHz   ┌────────┐
│   TrueNAS    │ ◄───disk.temps───► │  Container   │ ───"S<pct>"───► │  ESP32   │ ────────────► │ Ventola│
│   SCALE      │                    │  fan_control │ ◄───"RPM n"──── │          │ ◄───tacho──── │ 4-pin  │
└──────────────┘                    └──────────────┘                 └──────────┘               └────────┘
```

## Indice

- [Come funziona](#come-funziona)
- [Requisiti](#requisiti)
- [Hardware e cablaggio](#hardware-e-cablaggio)
- [Setup ESP32](#setup-esp32)
- [Setup TrueNAS](#setup-truenas)
- [Configurazione](#configurazione)
- [Verifica e troubleshooting](#verifica-e-troubleshooting)
- [Protocollo seriale](#protocollo-seriale)
- [Sicurezza (failsafe)](#sicurezza-failsafe)

## Come funziona

Il sistema ha due componenti che comunicano su una semplice seriale USB.

**Lato TrueNAS (`fan_control.py`, in container Docker)**

1. Si connette all'API JSON-RPC 2.0 over WebSocket di TrueNAS (`wss://host/api/current`) e si autentica con una API key.
2. Ogni `INTERVAL` secondi chiama il metodo `disk.temperatures` per ottenere le temperature dei dischi.
3. Prende la temperatura più alta tra i dischi monitorati e la converte in una percentuale di velocità tramite una **curva configurabile** (interpolazione lineare tra i punti).
4. Invia la percentuale all'ESP32 sulla seriale, ma solo quando cambia, per ridurre il traffico.
5. Legge le risposte e gli RPM riportati dall'ESP32 e li registra nei log.

**Lato ESP32 (`esp32_fan_control.ino`)**

1. Riceve la percentuale dalla seriale e genera un segnale PWM a 25 kHz (standard per ventole 4-pin) sul pin di controllo.
2. Conta gli impulsi del filo tacho della ventola tramite interrupt e calcola gli RPM una volta al secondo, inviandoli sulla seriale.
3. Include un **failsafe**: se non riceve comandi per 30 secondi, porta la ventola al 100%.

## Requisiti

**Software**

- TrueNAS SCALE **25.04 (Fangtooth) o successivo** (usa la nuova API WebSocket; le versioni precedenti con la vecchia REST API non sono supportate da questo script senza modifiche).
- Arduino IDE (o PlatformIO) con il core **Arduino-ESP32** installato. Lo sketch è compatibile sia con il core 2.x sia con il 3.x.

**Hardware**

- Una scheda ESP32 (qualsiasi modello con USB).
- Una ventola PWM **4-pin** (questo progetto è pensato per 5V; vale anche per ventole 12V cambiando solo l'alimentazione).
- Un'alimentazione adeguata per la ventola (vedi sotto).
- Un cavo USB tra ESP32 e il server TrueNAS.

## Hardware e cablaggio

Una ventola 4-pin ha questi fili:

| Filo | Colore tipico | Funzione |
|------|---------------|----------|
| GND | Nero | Massa |
| +V | Giallo/Rosso | Alimentazione |
| Tacho | Verde | Segnale RPM (open-collector) |
| PWM | Blu | Controllo velocità |

Collegamenti all'ESP32:

| Filo ventola | Collegamento |
|--------------|--------------|
| GND | GND dell'ESP32 **e** del'alimentazione (massa comune obbligatoria) |
| +V (5V) | Alimentazione 5V esterna |
| PWM (blu) | GPIO 25 |
| Tacho (verde) | GPIO 26 |

### Note importanti sul cablaggio

**Alimentazione.** Non alimentare la ventola dal pin 5V dell'ESP32 se assorbe più di ~100–200 mA: usa un'alimentazione 5V esterna (o il 5V del PC/USB) e collega la massa in comune con l'ESP32. Senza massa comune il segnale non funziona.

**Filo tacho e pull-up a 3.3V.** Il filo tacho è di tipo open-collector: internamente è un interruttore verso massa che si chiude due volte per giro. Per leggerlo serve un pull-up. **Deve essere riferito a 3.3V, mai a 5V**, perché i GPIO dell'ESP32 non sono 5V-tolerant e potrebbero danneggiarsi. Lo sketch usa il pull-up interno dell'ESP32 (`INPUT_PULLUP`, collegato ai 3.3V interni), quindi puoi collegare il filo tacho **direttamente** al GPIO 26 senza componenti esterni. Se il segnale risultasse instabile, aggiungi una resistenza esterna da ~10 kΩ tra il filo tacho e il pin **3.3V**.

**Livelli PWM.** Il segnale PWM dell'ESP32 è a 3.3V mentre lo standard delle ventole lo specifica a 5V. Nella stragrande maggioranza dei casi la ventola accetta comunque i 3.3V; se la tua non risponde, interponi un level shifter sul filo blu.

## Setup ESP32

1. Apri `esp32_fan_control.ino` in Arduino IDE.
2. Seleziona la tua scheda ESP32 e la porta corretta.
3. Carica lo sketch. Non serve scegliere la versione del core: lo sketch rileva da solo se usare le API LEDC del core 2.x o 3.x.
4. (Opzionale) Apri il Serial Monitor a **115200 baud** per verificare il funzionamento prima di collegare TrueNAS.

Parametri modificabili in cima allo sketch:

| Costante | Default | Descrizione |
|----------|---------|-------------|
| `PWM_PIN` | 25 | GPIO del segnale PWM |
| `TACHO_PIN` | 26 | GPIO del segnale tacho |
| `PWM_FREQ` | 25000 | Frequenza PWM in Hz |
| `PULSES_PER_REV` | 2 | Impulsi tacho per giro (alcune ventole: 1 o 4) |
| `WATCHDOG_MS` | 30000 | Tempo senza comandi prima del failsafe al 100% |

## Setup TrueNAS

### 1. Crea una API key

Nella web UI di TrueNAS: **Settings → API Keys → Add**. Dai un nome (es. `fan-control`) e **copia subito la chiave** (viene mostrata una sola volta).

### 2. Trova la porta seriale dell'ESP32

Dalla Shell di TrueNAS:

```bash
ls -l /dev/ttyUSB* /dev/ttyACM*
```

Tipicamente è `/dev/ttyUSB0` (chip CP210x/CH340) o `/dev/ttyACM0` (USB nativo). Annota il valore: serve sia nel `docker-compose.yml` (sezione `devices`) sia nella variabile `SERIAL_PORT`.

### 3. Copia i file su un pool

Crea una cartella su un tuo pool (dato che il filesystem di sistema è read-only e viene azzerato dagli aggiornamenti) e copiaci i tre file `fan_control.py`, `Dockerfile`, `docker-compose.yml`:

```bash
mkdir -p /mnt/tank/scripts/fan
# copia qui i file (scp, share SMB, git clone, ecc.)
```

Sostituisci `tank` con il nome del tuo pool.

### 4. Configura il compose

Apri `docker-compose.yml` e imposta almeno:

- `TN_APIKEY` → la API key creata al punto 1
- `devices` e `SERIAL_PORT` → la porta trovata al punto 2
- `DISKS` → i dischi da monitorare, oppure lascia vuoto per tutti

### 5. Avvia la Custom App

Due modi.

**Via UI:** **Apps → Discover Apps → Custom App** e incolla il contenuto del compose.

**Via Shell (build dell'immagine):**

```bash
cd /mnt/tank/scripts/fan
docker build -t fan-control .
docker compose up -d
```

> Nota: se usi l'installazione "via YAML" dalla UI, la direttiva `build: .` potrebbe non trovare il `Dockerfile`. In quel caso costruisci prima l'immagine con `docker build -t fan-control /mnt/tank/scripts/fan`, poi nel compose sostituisci `build: .` con `image: fan-control`.

## Configurazione

Tutte le opzioni si impostano tramite variabili d'ambiente nel `docker-compose.yml`.

| Variabile | Default | Descrizione |
|-----------|---------|-------------|
| `TN_HOST` | `host.docker.internal` | Host di TrueNAS. In alternativa l'IP fisso del NAS. |
| `TN_PORT` | `443` | Porta dell'API. |
| `TN_SSL` | `true` | Usa `wss://` (TLS). Il certificato self-signed non viene verificato. |
| `TN_APIKEY` | *(vuoto)* | **Obbligatoria.** API key creata nella UI. |
| `DISKS` | *(vuoto)* | Dischi da monitorare separati da virgola (es. `sda,sdb`). Vuoto = tutti. |
| `SERIAL_PORT` | `/dev/ttyUSB0` | Device seriale dell'ESP32. |
| `SERIAL_BAUD` | `115200` | Baud rate (deve combaciare con lo sketch). |
| `INTERVAL` | `10` | Secondi tra una lettura e l'altra. |
| `SAFE_DUTY` | `100` | Velocità % usata se la temperatura non è leggibile. |
| `CURVE` | `30:20,40:30,50:50,60:75,70:100` | Curva `temp:percentuale`, vedi sotto. |

### La curva di velocità

La curva è una lista di punti `temperatura:percentuale` separati da virgola. Con il default:

| Temp disco | Velocità ventola |
|------------|------------------|
| ≤ 30 °C | 20 % |
| 40 °C | 30 % |
| 50 °C | 50 % |
| 60 °C | 75 % |
| ≥ 70 °C | 100 % |

Tra un punto e l'altro la velocità viene interpolata linearmente. Sotto il primo punto resta al minimo, sopra l'ultimo resta al massimo. Adatta i valori al tuo hardware e al rumore che tolleri.

## Verifica e troubleshooting

**Test manuale dell'ESP32.** Con il Serial Monitor a 115200 baud, invia `S0`, `S50`, `S100`: dovresti vedere `OK S...` e la ventola cambiare velocità, più un flusso `RPM ...` ogni secondo.

**RPM sbagliati (doppi o dimezzati).** Aggiusta `PULSES_PER_REV` nello sketch (prova 1 o 4 invece di 2).

**Log del container.** Dalla UI nella pagina dell'app, oppure:

```bash
docker logs -f fan-control
```

Ad ogni ciclo lo script stampa le temperature grezze lette dall'API: utile per capire i nomi esatti dei dischi da mettere in `DISKS`.

**Il container non raggiunge TrueNAS.** Se `host.docker.internal` non funziona, sostituisci `TN_HOST` con l'IP fisso del NAS nella stessa LAN.

**Nessuna temperatura valida.** Lo script mette la ventola a `SAFE_DUTY` (100% di default) e tenta la riconnessione all'API. Controlla che la API key sia valida e che i nomi in `DISKS` corrispondano a quelli nei log.

## Protocollo seriale

Comunicazione testuale, terminata da newline, a 115200 baud.

**Dal controller all'ESP32:**

| Comando | Effetto |
|---------|---------|
| `S<0-100>\n` | Imposta la velocità in percentuale (es. `S75`). |

**Dall'ESP32 al controller:**

| Messaggio | Significato |
|-----------|-------------|
| `READY` | Inviato una volta all'avvio. |
| `OK S<pct>` | Conferma di un comando ricevuto. |
| `RPM <n>` | Velocità misurata della ventola, una volta al secondo. |

## Sicurezza (failsafe)

Il sistema ha due livelli di protezione indipendenti per non lasciare mai i dischi senza raffreddamento:

- **Lato ESP32:** se non riceve comandi per `WATCHDOG_MS` (30 s di default) — ad esempio per crash del container o cavo USB staccato — porta la ventola al 100% finché non torna un comando valido.
- **Lato script:** se l'API non risponde o non fornisce temperature valide, invia `SAFE_DUTY` (100% di default) e tenta la riconnessione.

## Licenza

Rilascia il progetto sotto la licenza che preferisci (es. MIT). Aggiungi qui il file `LICENSE` corrispondente.
