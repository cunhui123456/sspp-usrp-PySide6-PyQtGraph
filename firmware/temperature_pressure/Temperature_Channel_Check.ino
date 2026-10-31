#include <Wire.h>

// Experimental voltage check, derived from the supplied acquisition sketch.
// S6 = TCA9555 P5 (bit 5), S7 = P6 (bit 6); both use the T -> U7 -> A1 path.
// Voltage uses nominal AVcc = 5.0 V. No Celsius calibration is available.
const uint8_t TCA_ADDRESS = 0x20;
const uint8_t OUTPUT_PORT0 = 0x02;
const uint8_t OUTPUT_PORT1 = 0x03;
const uint8_t CONFIG_PORT0 = 0x06;
const uint8_t CONFIG_PORT1 = 0x07;
const uint8_t SAMPLE_COUNT = 16;
const unsigned long SETTLE_MS = 20;
const float NOMINAL_ADC_REFERENCE_V = 5.0f;
bool initialized = false;

bool writeRegister(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(TCA_ADDRESS);
  Wire.write(reg);
  Wire.write(value);
  return Wire.endTransmission() == 0;
}

bool readTemperatureChannel(uint8_t bitIndex, float &meanAdc) {
  // Only the two documented temperature selections are permitted here.
  if (bitIndex != 5 && bitIndex != 6) return false;
  if (!writeRegister(OUTPUT_PORT0, 0)) return false;
  if (!writeRegister(OUTPUT_PORT0, uint8_t(1U << bitIndex))) return false;
  delay(SETTLE_MS);
  analogRead(A1);  // Discard the first conversion after changing the channel.
  uint32_t total = 0;
  for (uint8_t i = 0; i < SAMPLE_COUNT; ++i) {
    total += analogRead(A1);
    delay(1);
  }
  meanAdc = float(total) / SAMPLE_COUNT;
  return writeRegister(OUTPUT_PORT0, 0);
}

void setup() {
  Serial.begin(9600);
  Wire.begin();
  Wire.setWireTimeout(25000, true);
  // Set output latches low before enabling output drivers.
  initialized = writeRegister(OUTPUT_PORT0, 0)
             && writeRegister(OUTPUT_PORT1, 0)
             && writeRegister(CONFIG_PORT0, 0)
             && writeRegister(CONFIG_PORT1, 0);
  Serial.println(F("# Temperature_Channel_Check v1; A1; nominal_Vref=5.0V; no_Celsius_calibration"));
  Serial.println(F("# S6=P5,S7=P6; sequential; settle_ms=20; samples=16; original_firmware_replaced"));
  if (!initialized) {
    Serial.println(F("# ERROR: TCA9555 initialization failed at 0x20; no valid measurements"));
    return;
  }
  Serial.println(F("t6_ms,S6_adc,S6_V,t7_ms,S7_adc,S7_V"));
}

void loop() {
  if (!initialized) {
    delay(1000);
    return;
  }
  float s6Adc = 0;
  float s7Adc = 0;
  if (!readTemperatureChannel(5, s6Adc)) {
    writeRegister(OUTPUT_PORT0, 0);
    Serial.println(F("# ERROR: S6 selection failed; row omitted"));
    delay(1000);
    return;
  }
  const unsigned long t6 = millis();
  if (!readTemperatureChannel(6, s7Adc)) {
    writeRegister(OUTPUT_PORT0, 0);
    Serial.println(F("# ERROR: S7 selection failed; row omitted"));
    delay(1000);
    return;
  }
  const unsigned long t7 = millis();
  Serial.print(t6);
  Serial.print(',');
  Serial.print(s6Adc, 2);
  Serial.print(',');
  Serial.print(s6Adc * NOMINAL_ADC_REFERENCE_V / 1023.0f, 3);
  Serial.print(',');
  Serial.print(t7);
  Serial.print(',');
  Serial.print(s7Adc, 2);
  Serial.print(',');
  Serial.println(s7Adc * NOMINAL_ADC_REFERENCE_V / 1023.0f, 3);
  delay(100);
}
