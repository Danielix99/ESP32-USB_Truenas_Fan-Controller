/* Copyright (C) 2026  Daniele Rossi

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.

 * Controllo ventola 4-pin 5V con ESP32
 * - Riceve da seriale: "S<0-100>\n"  -> imposta il duty PWM in percentuale
 * - Invia su seriale:  "RPM <valore>\n" una volta al secondo
 * - Failsafe: se non riceve comandi per WATCHDOG_MS, mette la ventola al 100%
 *
 * Compatibile sia con core Arduino-ESP32 2.x sia 3.x (vedi blocco condizionale).
 *
 * Collegamenti:
 *   GND ventola  -> GND ESP32 (massa comune obbligatoria)
 *   +5V ventola  -> alimentazione 5V esterna (NON dal pin dell'ESP32 se >100-200mA)
 *   PWM  (blu)   -> GPIO 25
 *   Tacho(verde) -> GPIO 26  (open-collector, pull-up a 3.3V)
 */

#include <Arduino.h>

// --- Pin ---
const int PWM_PIN   = 25;   // filo blu  (PWM control)
const int TACHO_PIN = 26;   // filo verde (RPM)

// --- PWM ---
const int PWM_FREQ    = 25000;  // 25 kHz, standard ventole 4-pin
const int PWM_RES     = 8;      // risoluzione 8 bit -> 0..255
const int PWM_CHANNEL = 0;      // usato solo dal core 2.x

// --- Tacho ---
const int PULSES_PER_REV = 2;   // la maggior parte delle ventole: 2 impulsi/giro
volatile unsigned long pulseCount = 0;
unsigned long lastRpmCalc = 0;
int currentRpm = 0;

// --- Failsafe ---
const unsigned long WATCHDOG_MS = 30000; // se nessun comando entro 30s -> 100%
unsigned long lastCmdTime = 0;

void IRAM_ATTR onTachoPulse() {
  pulseCount++;
}

// Wrapper per gestire le due API diverse (core 2.x vs 3.x)
void pwmSetup() {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcAttach(PWM_PIN, PWM_FREQ, PWM_RES);
#else
  ledcSetup(PWM_CHANNEL, PWM_FREQ, PWM_RES);
  ledcAttachPin(PWM_PIN, PWM_CHANNEL);
#endif
}

void pwmWriteDuty(int duty) {  // duty 0..255
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcWrite(PWM_PIN, duty);
#else
  ledcWrite(PWM_CHANNEL, duty);
#endif
}

void setDutyPercent(int pct) {
  pct = constrain(pct, 0, 100);
  int duty = map(pct, 0, 100, 0, 255);
  pwmWriteDuty(duty);
}

void setup() {
  Serial.begin(115200);

  pwmSetup();
  setDutyPercent(50);          // 50% all'avvio finche' non arriva un comando

  pinMode(TACHO_PIN, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(TACHO_PIN), onTachoPulse, FALLING);

  lastCmdTime = millis();
  Serial.println("READY");
}

void loop() {
  // --- Ricezione comandi ---
  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    line.trim();
    if (line.startsWith("S")) {
      int pct = line.substring(1).toInt();
      setDutyPercent(pct);
      lastCmdTime = millis();
      Serial.print("OK S");
      Serial.println(constrain(pct, 0, 100));
    }
  }

  unsigned long now = millis();

  // --- Calcolo RPM ogni secondo ---
  if (now - lastRpmCalc >= 1000) {
    noInterrupts();
    unsigned long pulses = pulseCount;
    pulseCount = 0;
    interrupts();

    currentRpm = (pulses * 60) / PULSES_PER_REV;
    lastRpmCalc = now;

    Serial.print("RPM ");
    Serial.println(currentRpm);
  }

  // --- Failsafe: nessun comando da troppo tempo -> ventola al massimo ---
  if (now - lastCmdTime > WATCHDOG_MS) {
    setDutyPercent(100);
    // non aggiorno lastCmdTime: resta al 100% finche' non torna un comando valido
  }
}
