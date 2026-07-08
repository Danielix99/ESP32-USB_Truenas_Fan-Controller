# ESP32 Disk-Temp Fan Controller for TrueNAS

PWM speed control of a 4-pin fan via an ESP32, driven by the disk temperatures of a **TrueNAS SCALE** server.

The script runs in a Docker container on TrueNAS, reads the disk temperatures through the official API, and sends the ESP32 (over USB serial) the speed percentage computed from a configurable curve. The ESP32 translates that percentage into a 25 kHz PWM signal and reads the fan's RPM in real time.

```
┌──────────────┐   API WebSocket    ┌──────────────┐   USB serial    ┌──────────┐   PWM 25kHz   ┌────────┐
│   TrueNAS    │ ◄───disk.temps───► │  Container   │ ───"S<pct>"───► │  ESP32   │ ────────────► │  Fan   │
│   SCALE      │                    │  fan_control │ ◄───"RPM n"──── │          │ ◄───tacho──── │ 4-pin  │
└──────────────┘                    └──────────────┘                 └──────────┘               └────────┘
```

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Hardware and wiring](#hardware-and-wiring)
- [ESP32 setup](#esp32-setup)
- [TrueNAS setup](#truenas-setup)
- [Configuration](#configuration)
- [Verification and troubleshooting](#verification-and-troubleshooting)
- [Serial protocol](#serial-protocol)
- [Safety (failsafe)](#safety-failsafe)

## How it works

The system has two components that communicate over a simple USB serial link.

**TrueNAS side (`fan_control.py`, in a Docker container)**

1. Connects to TrueNAS's JSON-RPC 2.0 over WebSocket API (`wss://host/api/current`) and authenticates with an API key.
2. Every `INTERVAL` seconds it calls the `disk.temperatures` method to get the disk temperatures.
3. It takes the highest temperature among the monitored disks and converts it into a speed percentage via a **configurable curve** (linear interpolation between points).
4. It sends the percentage to the ESP32 over serial, but only when it changes, to reduce traffic.
5. It reads the responses and the RPM reported by the ESP32 and records them in the logs.

**ESP32 side (`esp32_fan_control.ino`)**

1. Receives the percentage over serial and generates a 25 kHz PWM signal (standard for 4-pin fans) on the control pin.
2. Counts the fan's tacho wire pulses via interrupt and computes the RPM once per second, sending them over serial.
3. Includes a **failsafe**: if it receives no commands for 30 seconds, it brings the fan to 100%.

## Requirements

**Software**

- TrueNAS SCALE **25.04 (Fangtooth) or later** (uses the new WebSocket API; earlier versions with the old REST API are not supported by this script without modifications).
- Arduino IDE (or PlatformIO) with the **Arduino-ESP32** core installed. The sketch is compatible with both the 2.x and 3.x core.

**Hardware**

- An ESP32 board (any model with USB).
- A **4-pin** PWM fan (this project is designed for 5V but has been used successfully with 12V fans).
- A ceramic capacitor of about 10nF to connect between Tach and GND (it filters out the PWM spurious signal that couples back into the Tach on many ESP32s).
- An adequate power supply for the fan (see below).
- A USB cable between the ESP32 and the TrueNAS server.

## Hardware and wiring

A 4-pin fan has these wires:

| Wire | Typical color | Function |
|------|---------------|----------|
| GND | Black | Ground |
| +V | Yellow/Red | Power |
| Tacho | Green | RPM signal (open-collector) |
| PWM | Blue | Speed control |

Connections to the ESP32:

| Fan wire | Connection |
|----------|------------|
| GND | ESP32 GND **and** power supply GND (common ground required) |
| +V (5V) | External 5V power supply |
| PWM (blue) | GPIO 25 |
| Tacho (green) | GPIO 26 |

### Important wiring notes

**Power.** Do not power the fan from the ESP32's 5V pin: use an external power supply (5V from the PC/USB, 12V from a disk Molex connector) and share the ground with the ESP32. Without a common ground the signal may not work.

**Tacho wire and 3.3V pull-up.** The tacho wire is open-collector: internally it is a switch to ground that closes twice per revolution. To read it you need a pull-up. **It must be referenced to 3.3V, never to 5V**, because the ESP32 GPIOs are not 5V-tolerant and could be damaged. The sketch uses the ESP32's internal pull-up (`INPUT_PULLUP`, connected to the internal 3.3V), so you can connect the tacho wire **directly** to GPIO 26 without external components. If the signal turns out unstable, add an external ~10 kΩ resistor between the tacho wire and the **3.3V** pin. Connect a ceramic capacitor of about 10nF between Tach and GND to ensure correct RPM readings.

**PWM levels.** The ESP32's PWM signal is 3.3V, whereas the fan standard specifies 5V/12V. In the vast majority of cases the fan accepts 3.3V anyway; if yours does not respond, insert a level shifter on the blue wire.

## ESP32 setup

1. Open `esp32_fan_control.ino` in the Arduino IDE.
2. Select your ESP32 board and the correct port.
3. Upload the sketch. You do not need to choose the core version: the sketch auto-detects whether to use the 2.x or 3.x core's LEDC APIs.
4. (Optional) Open the Serial Monitor at **115200 baud** to verify operation before connecting TrueNAS.

Parameters you can change at the top of the sketch:

| Constant | Default | Description |
|----------|---------|-------------|
| `PWM_PIN` | 25 | GPIO of the PWM signal |
| `TACHO_PIN` | 26 | GPIO of the tacho signal |
| `PWM_FREQ` | 25000 | PWM frequency in Hz |
| `PULSES_PER_REV` | 2 | Tacho pulses per revolution (some fans: 1 or 4) |
| `WATCHDOG_MS` | 30000 | Time without commands before the failsafe to 100% |

## TrueNAS setup

### 1. Create an API key

In the TrueNAS web UI: **Settings → API Keys → Add**. Give it a name (e.g. `fan-control`) and **copy the key immediately** (it is shown only once).

### 2. Find the ESP32's serial port

From the TrueNAS Shell:

```bash
ls -l /dev/ttyUSB* /dev/ttyACM*
```

Typically it is `/dev/ttyUSB0` (CP210x/CH340 chip) or `/dev/ttyACM0` (native USB). Note the value: you need it both in the `docker-compose.yml` (the `devices` section) and in the `SERIAL_PORT` variable.

### 3. Copy the files to a pool

Create a folder on one of your pools (since the system filesystem is read-only and is wiped by updates) and copy the three files `fan_control.py`, `Dockerfile`, `docker-compose.yml` into it:

```bash
mkdir -p /mnt/tank/scripts/fan
# copy the files here (scp, SMB share, git clone, etc.)
```

Replace `tank` with the name of your pool.

### 4. Configure the compose

Open `docker-compose.yml` and set at least:

- `TN_APIKEY` → the API key created in step 1
- `devices` and `SERIAL_PORT` → the port found in step 2
- `DISKS` → the disks to monitor, or leave empty for all

### 5. Start the Custom App

Two ways.

**Via UI:** **Apps → Discover Apps → Custom App** and paste the contents of the compose.

**Via Shell (building the image):**

```bash
cd /mnt/tank/scripts/fan
docker build -t fan-control .
docker compose up -d
```

> Note: if you use the "via YAML" installation from the UI, the `build: .` directive may not find the `Dockerfile`. In that case build the image first with `docker build -t fan-control /mnt/tank/scripts/fan`, then in the compose replace `build: .` with `image: fan-control`.

## Configuration

All options are set through environment variables in `docker-compose.yml`.

| Variable | Default | Description |
|----------|---------|-------------|
| `TN_HOST` | `host.docker.internal` | TrueNAS host. Alternatively, the NAS's fixed IP. |
| `TN_PORT` | `443` | API port. |
| `TN_SSL` | `true` | Use `wss://` (TLS). The self-signed certificate is not verified. |
| `TN_APIKEY` | *(empty)* | **Required.** API key created in the UI. |
| `DISKS` | *(empty)* | Disks to monitor, comma-separated (e.g. `sda,sdb`). Empty = all. |
| `SERIAL_PORT` | `/dev/ttyUSB0` | ESP32 serial device. |
| `SERIAL_BAUD` | `115200` | Baud rate (must match the sketch). |
| `INTERVAL` | `10` | Seconds between readings. |
| `SAFE_DUTY` | `100` | Speed % used if the temperature can't be read. |
| `CURVE` | `30:20,40:30,50:50,60:75,70:100` | `temp:percentage` curve, see below. |

### The speed curve

The curve is a comma-separated list of `temperature:percentage` points. With the default:

| Disk temp | Fan speed |
|-----------|-----------|
| ≤ 30 °C | 20 % |
| 40 °C | 30 % |
| 50 °C | 50 % |
| 60 °C | 75 % |
| ≥ 70 °C | 100 % |

Between one point and the next the speed is linearly interpolated. Below the first point it stays at the minimum, above the last point it stays at the maximum. Adjust the values to your hardware and the noise you can tolerate.

## Verification and troubleshooting

**Manual ESP32 test.** With the Serial Monitor at 115200 baud, send `S0`, `S50`, `S100`: you should see `OK S...` and the fan changing speed, plus an `RPM ...` stream every second.

**Wrong RPM (doubled or halved).** Adjust `PULSES_PER_REV` in the sketch (try 1 or 4 instead of 2).

**Container logs.** From the UI on the app's page, or:

```bash
docker logs -f fan-control
```

On each cycle the script prints the raw temperatures read from the API: useful for figuring out the exact disk names to put in `DISKS`.

**The container can't reach TrueNAS.** If `host.docker.internal` doesn't work, replace `TN_HOST` with the NAS's fixed IP on the same LAN.

**No valid temperature.** The script sets the fan to `SAFE_DUTY` (100% by default) and attempts to reconnect to the API. Check that the API key is valid and that the names in `DISKS` match those in the logs.

## Serial protocol

Text-based communication, newline-terminated, at 115200 baud.

**From the controller to the ESP32:**

| Command | Effect |
|---------|--------|
| `S<0-100>\n` | Set the speed as a percentage (e.g. `S75`). |

**From the ESP32 to the controller:**

| Message | Meaning |
|---------|---------|
| `READY` | Sent once at startup. |
| `OK S<pct>` | Acknowledgment of a received command. |
| `RPM <n>` | Measured fan speed, once per second. |

## Safety (failsafe)

The system has two independent protection layers to never leave the disks without cooling:

- **ESP32 side:** if it receives no commands for `WATCHDOG_MS` (30 s by default) — for example due to a container crash or an unplugged USB cable — it brings the fan to 100% until a valid command returns.
- **Script side:** if the API doesn't respond or doesn't provide valid temperatures, it sends `SAFE_DUTY` (100% by default) and attempts to reconnect.


## TODO
Work in progress with developments still to come:

- Add a web server
- Add a WebUI
- Control the curve and the fan directly through the GUI
- Fan control based also on RPM feedback
