#define RELAY_PIN 7
#define RELAY_ON LOW       // Common active-LOW relay modules
#define RELAY_OFF HIGH

String command = "";

void setup() {
  digitalWrite(RELAY_PIN, RELAY_OFF);
  pinMode(RELAY_PIN, OUTPUT);
  Serial.begin(9600);
}

void loop() {
  while (Serial.available() > 0) {
    const char c = (char)Serial.read();
    if (c == '\n') {
      command.trim();
      if (command == "ACCESS_GRANTED") {
        digitalWrite(RELAY_PIN, RELAY_ON);
        Serial.println("DOOR_UNLOCKED");
        delay(2000);
        digitalWrite(RELAY_PIN, RELAY_OFF);
        Serial.println("DOOR_LOCKED");
      }
      command = "";
    } else if (c != '\r' && command.length() < 40) {
      command += c;
    }
  }
}
