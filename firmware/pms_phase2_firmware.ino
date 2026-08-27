/*
  PATIENT MONITORING SYSTEM (PMS) - Phase 2 Firmware
  Hardware: ESP8266 (NodeMCU) + MAX30102
  --------------------------------------------------------------
  What changed from Phase 1:
  - Removed the serial "enter patient age" prompt. Age is now
    entered in the GUI on the laptop, not on the device.
  - Removed the on-device smoothing filter and diagnosis text
    (Normal/Tachycardia/etc). Those now live in Python, where
    they can also feed the RR, BP, and AI early-warning modules.
  - The ESP8266's only job now: read the sensor, compute HR/SpO2
    exactly as Phase 1 did, and stream everything (raw samples +
    computed vitals) to the laptop over serial.

  Serial protocol (one message per line, comma-separated):
    R,<ir>,<red>                          -> one raw sample, sent as it's read
    V,<hr>,<hrValid>,<spo2>,<spo2Valid>   -> latest computed vitals (~once/sec)
    E,<message>                            -> status/error events

  Baud rate: 115200
  Wiring: unchanged from Phase 1 (SDA->D2, SCL->D1, VIN->3.3V, GND->GND)
*/

#include <Wire.h>
#include "MAX30105.h"
#include "spo2_algorithm.h"

MAX30105 particleSensor;

uint32_t irBuffer[100];
uint32_t redBuffer[100];
int32_t bufferLength;
int32_t spo2;
int8_t validSPO2;
int32_t heartRate;
int8_t validHeartRate;

void sendRawSample(uint32_t ir, uint32_t red) {
  Serial.print(F("R,"));
  Serial.print(ir);
  Serial.print(F(","));
  Serial.println(red);
}

void sendVitals() {
  Serial.print(F("V,"));
  Serial.print(heartRate);
  Serial.print(F(","));
  Serial.print(validHeartRate);
  Serial.print(F(","));
  Serial.print(spo2);
  Serial.print(F(","));
  Serial.println(validSPO2);
}

void setup() {
  Serial.begin(115200);
  delay(1000);

  if (!particleSensor.begin(Wire, I2C_SPEED_FAST)) {
    Serial.println(F("E,SENSOR_NOT_FOUND"));
    while (1);
  }

  particleSensor.setup(60, 4, 2, 100, 411, 4096);
  Serial.println(F("E,READY"));
}

void loop() {
  bufferLength = 100;

  // Initial fill of 100 samples (same as Phase 1)
  for (byte i = 0; i < bufferLength; i++) {
    while (particleSensor.available() == false)
      particleSensor.check();
    redBuffer[i] = particleSensor.getRed();
    irBuffer[i] = particleSensor.getIR();
    particleSensor.nextSample();
    sendRawSample(irBuffer[i], redBuffer[i]);
  }

  maxim_heart_rate_and_oxygen_saturation(irBuffer, bufferLength, redBuffer, &spo2, &validSPO2, &heartRate, &validHeartRate);

  while (1) {
    // Shift buffer left by 25 (same as Phase 1)
    for (byte i = 25; i < 100; i++) {
      redBuffer[i - 25] = redBuffer[i];
      irBuffer[i - 25] = irBuffer[i];
    }

    // Collect 25 new samples, streaming each one as it arrives
    for (byte i = 75; i < 100; i++) {
      while (particleSensor.available() == false)
        particleSensor.check();
      redBuffer[i] = particleSensor.getRed();
      irBuffer[i] = particleSensor.getIR();
      particleSensor.nextSample();
      sendRawSample(irBuffer[i], redBuffer[i]);
    }

    maxim_heart_rate_and_oxygen_saturation(irBuffer, bufferLength, redBuffer, &spo2, &validSPO2, &heartRate, &validHeartRate);

    if (irBuffer[99] < 50000) {
      Serial.println(F("E,NO_CONTACT"));
    } else {
      sendVitals();
    }
  }
}
