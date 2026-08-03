#include <WiFi.h>
#include <WebServer.h>

// ================= WIFI CREDENTIALS =================
const char* ssid     = "Smatt";
const char* password = "8394229388583201";

// ================= STATIC IP CONFIGURATION =================
IPAddress local_IP(192, 168, 1, 26);   // Fixed IP for Button DevKit
IPAddress gateway(192, 168, 1, 1);    // Router IP
IPAddress subnet(255, 255, 255, 0);   // Subnet
IPAddress primaryDNS(8, 8, 8, 8);     // Google DNS

// ================= HARDWARE PINS ====================
const int BTN_MOMENTARY_PIN = 4;  // GPIO 4 connected to Push Button
const int BTN_TOGGLE_PIN    = 5;  // GPIO 5 connected to Toggle Switch

WebServer server(8080);

void handleButtons() {
  // Using INPUT_PULLUP logic:
  // Active / Pressed / ON  == LOW  (0V connected to GND) -> Returns true
  // Inactive / Released / OFF == HIGH (3.3V pulled up)  -> Returns false
  bool momentary_state = (digitalRead(BTN_MOMENTARY_PIN) == LOW);
  bool toggle_state    = (digitalRead(BTN_TOGGLE_PIN) == LOW);

  String jsonResponse = "{";
  jsonResponse += "\"btn_momentary\":" + String(momentary_state ? "true" : "false") + ",";
  jsonResponse += "\"btn_toggle\":" + String(toggle_state ? "true" : "false");
  jsonResponse += "}";

  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.send(200, "application/json", jsonResponse);
}

void handleNotFound() {
  server.send(404, "text/plain", "Not Found");
}

void setup() {
  Serial.begin(115200);
  delay(500);

  // Enable reliable internal pull-up resistors on DevKit pins
  pinMode(BTN_MOMENTARY_PIN, INPUT_PULLUP);
  pinMode(BTN_TOGGLE_PIN, INPUT_PULLUP);

  if (!WiFi.config(local_IP, gateway, subnet, primaryDNS)) {
    Serial.println("❌ Static IP Config Failed!");
  }

  WiFi.begin(ssid, password);
  Serial.print("Connecting to WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }

  Serial.println("\n✅ DevKit WiFi Connected!");
  Serial.print("📍 Fixed Button Server Endpoint: http://");
  Serial.print(WiFi.localIP());
  Serial.println(":8080/buttons");

  server.on("/buttons", HTTP_GET, handleButtons);
  server.onNotFound(handleNotFound);

  server.begin();
  Serial.println("🚀 DevKit Server listening on Port 8080.");
}

void loop() {
  server.handleClient();
}