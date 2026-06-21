# Copyright (C) 2026  Daniele Rossi
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#!/usr/bin/env python3
"""
Controllo ventola ESP32 basato sulla temperatura disco di TrueNAS SCALE (25.04+).

- Legge la temperatura disco via API JSON-RPC 2.0 over WebSocket (metodo disk.temperatures)
- Calcola il duty cycle con una curva temperatura -> velocita'
- Invia il comando all'ESP32 sulla seriale USB ("S<percentuale>\n")
- Legge le risposte/RPM dall'ESP32

Config via variabili d'ambiente (impostate nel docker-compose).
"""

import json
import os
import sys
import time
import ssl

import serial          # pyserial
import websocket       # websocket-client

# ----------------- Config (da env) -----------------
TN_HOST   = os.environ.get("TN_HOST", "host.docker.internal")  # IP/host di TrueNAS
TN_PORT   = os.environ.get("TN_PORT", "443")
TN_APIKEY = os.environ.get("TN_APIKEY", "")                    # API key creata nella UI
TN_SSL    = os.environ.get("TN_SSL", "true").lower() == "true" # usa wss:// se true

# Dischi da monitorare, separati da virgola. Vuoto = tutti i dischi del sistema.
# Usa i nomi tipo "sda,sdb" oppure "nvme0n1".
DISKS = [d.strip() for d in os.environ.get("DISKS", "").split(",") if d.strip()]

SERIAL_PORT = os.environ.get("SERIAL_PORT", "/dev/ttyUSB0")
SERIAL_BAUD = int(os.environ.get("SERIAL_BAUD", "115200"))
INTERVAL    = int(os.environ.get("INTERVAL", "10"))           # secondi tra i cicli

# Curva: lista "temp:duty" separata da virgola. Esempio: "30:20,40:30,50:50,60:75,70:100"
def parse_curve(s):
    pts = []
    for item in s.split(","):
        t, d = item.split(":")
        pts.append((float(t), int(d)))
    return sorted(pts)

CURVE = parse_curve(os.environ.get("CURVE", "30:20,40:30,50:50,60:75,70:100"))

# Duty di sicurezza se non riusciamo a leggere la temperatura
SAFE_DUTY = int(os.environ.get("SAFE_DUTY", "100"))


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


# ----------------- API TrueNAS -----------------
class TrueNASClient:
    def __init__(self):
        self.ws = None
        self._id = 0

    def _next_id(self):
        self._id += 1
        return self._id

    def connect(self):
        scheme = "wss" if TN_SSL else "ws"
        url = f"{scheme}://{TN_HOST}:{TN_PORT}/api/current"
        sslopt = {"cert_reqs": ssl.CERT_NONE} if TN_SSL else None
        self.ws = websocket.create_connection(url, sslopt=sslopt, timeout=30)
        # autenticazione con API key
        res = self.call("auth.login_with_api_key", [TN_APIKEY])
        if res is not True:
            raise RuntimeError(f"Autenticazione fallita: {res}")
        log("Autenticato all'API TrueNAS")

    def call(self, method, params=None):
        msg = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params or [],
        }
        self.ws.send(json.dumps(msg))
        # attende la risposta con lo stesso id (ignora eventuali notifiche)
        while True:
            raw = self.ws.recv()
            data = json.loads(raw)
            if data.get("id") == msg["id"]:
                if "error" in data:
                    raise RuntimeError(data["error"])
                return data.get("result")

    def disk_temperatures(self, disks):
        # disk.temperatures accetta la lista di dischi; lista vuota = tutti
        return self.call("disk.temperatures", [disks])

    def close(self):
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass


# ----------------- Logica -----------------
def temp_to_duty(temp):
    if temp <= CURVE[0][0]:
        return CURVE[0][1]
    if temp >= CURVE[-1][0]:
        return CURVE[-1][1]
    for i in range(len(CURVE) - 1):
        t0, d0 = CURVE[i]
        t1, d1 = CURVE[i + 1]
        if t0 <= temp <= t1:
            ratio = (temp - t0) / (t1 - t0)
            return int(round(d0 + ratio * (d1 - d0)))
    return CURVE[-1][1]


def open_serial():
    ser = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=1)
    time.sleep(2)  # attende il reset dell'ESP32 all'apertura porta
    return ser


def main():
    if not TN_APIKEY:
        log("ERRORE: TN_APIKEY non impostata")
        sys.exit(1)

    ser = open_serial()
    log(f"Seriale aperta su {SERIAL_PORT} @ {SERIAL_BAUD}")

    tn = TrueNASClient()
    tn.connect()

    last_duty = None
    while True:
        try:
            temps = tn.disk_temperatures(DISKS)  # dict {disk: temp_o_None}
            valid = [v for v in temps.values() if isinstance(v, (int, float))]
            if valid:
                temp = max(valid)  # usa la piu' calda
                duty = temp_to_duty(temp)
                log(f"Dischi: {temps} | max={temp}C -> ventola {duty}%")
            else:
                temp = None
                duty = SAFE_DUTY
                log(f"Nessuna temp valida ({temps}) -> ventola {duty}% (sicurezza)")
        except Exception as e:
            log(f"Errore lettura API: {e} -> riconnessione")
            try:
                tn.close()
                tn.connect()
            except Exception as e2:
                log(f"Riconnessione fallita: {e2}")
            duty = SAFE_DUTY

        # invia solo se cambiato (riduce traffico seriale)
        if duty != last_duty:
            try:
                ser.write(f"S{duty}\n".encode())
                last_duty = duty
            except Exception as e:
                log(f"Errore scrittura seriale: {e} -> riapro porta")
                try:
                    ser.close()
                except Exception:
                    pass
                time.sleep(2)
                ser = open_serial()

        # legge eventuali risposte/RPM dall'ESP32
        try:
            while ser.in_waiting:
                line = ser.readline().decode(errors="ignore").strip()
                if line:
                    log(f"ESP32: {line}")
        except Exception:
            pass

        time.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Interrotto")
