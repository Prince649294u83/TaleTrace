#include <WiFi.h>
#include <WebServer.h>
#include <U8g2lib.h>
#include <Wire.h>

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

// ================= OLED SETUP (SH1106 I2C) =================
U8G2_SH1106_128X64_NONAME_F_HW_I2C u8g2(U8G2_R0, /* reset=*/ U8X8_PIN_NONE);

String currentText = "Ready for Backend!";
String formattedLines[40];    // Holds up to 40 wrapped lines
int totalLines = 0;
int currentLineIndex = 0;      // Controls manual scroll position
const int VISIBLE_LINES = 4;   // Lines displayed per page

// Button State Management & Debounce for Dual-Press
bool lastBothState = false;
unsigned long lastDebounceTime = 0;
const unsigned long DEBOUNCE_DELAY = 250; // 250ms debounce window

WebServer server(8080);

// Format raw string into lines that fit the display width
int prepareTextLines(String text) {
  u8g2.setFont(u8g2_font_profont12_tf);
  const int lineLength = 18; // ~18 characters per line
  String remaining = text;
  int count = 0;

  while (remaining.length() > 0 && count < 40) {
    if (remaining.length() <= lineLength) {
      formattedLines[count++] = remaining;
      break;
    }

    int splitIndex = remaining.lastIndexOf(' ', lineLength);
    if (splitIndex == -1 || splitIndex == 0) {
      splitIndex = lineLength;
    }

    formattedLines[count++] = remaining.substring(0, splitIndex);
    remaining = remaining.substring(splitIndex);
    remaining.trim();
  }

  return count;
}

void renderCurrentPage() {
  u8g2.clearBuffer();
  u8g2.setFont(u8g2_font_profont12_tf);

  int startY = 14;
  int lineHeight = 14;

  for (int i = 0; i < VISIBLE_LINES; i++) {
    int lineToDraw = currentLineIndex + i;
    if (lineToDraw < totalLines) {
      u8g2.drawStr(0, startY + (i * lineHeight), formattedLines[lineToDraw].c_str());
    }
  }

  u8g2.sendBuffer();
}

// Update text on OLED and reset view to line 0
void updateOLED(String text) {
  currentText = text;
  totalLines = prepareTextLines(currentText);
  currentLineIndex = 0; // Reset position to top
  renderCurrentPage();
}

// Scroll to next page of lines, or loop back to top
void scrollOLEDNext() {
  if (totalLines <= VISIBLE_LINES) return; // No scroll needed if short text

  currentLineIndex += VISIBLE_LINES;

  // Jump back to start if we exceed total lines
  if (currentLineIndex >= totalLines) {
    currentLineIndex = 0;
  }

  renderCurrentPage();
}

// Check hardware buttons and handle scrolling when both are active
void checkScrollTrigger() {
  bool momentary_state = (digitalRead(BTN_MOMENTARY_PIN) == LOW);
  bool toggle_state    = (digitalRead(BTN_TOGGLE_PIN) == LOW);
  bool currentBothState = (momentary_state && toggle_state);

  // Trigger scroll on transition from NOT active -> BOTH ACTIVE
  if (currentBothState && !lastBothState) {
    if ((millis() - lastDebounceTime) > DEBOUNCE_DELAY) {
      scrollOLEDNext();
      lastDebounceTime = millis();
    }
  }
  
  lastBothState = currentBothState;
}

// GET /buttons -> Returns JSON button states
void handleButtons() {
  bool momentary_state = (digitalRead(BTN_MOMENTARY_PIN) == LOW);
  bool toggle_state    = (digitalRead(BTN_TOGGLE_PIN) == LOW);
  bool both_state      = (momentary_state && toggle_state);

  String jsonResponse = "{";
  jsonResponse += "\"btn_momentary\":" + String(momentary_state ? "true" : "false") + ",";
  jsonResponse += "\"btn_toggle\":" + String(toggle_state ? "true" : "false") + ",";
  jsonResponse += "\"both_active\":" + String(both_state ? "true" : "false");
  jsonResponse += "}";

  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.send(200, "application/json", jsonResponse);
}

// GET /health -> Returns diagnostic device telemetry
void handleHealth() {
  String jsonResponse = "{";
  jsonResponse += "\"device\":\"button_oled\",";
  jsonResponse += "\"protocol_version\":1,";
  jsonResponse += "\"firmware_version\":\"1.2.0\",";
  jsonResponse += "\"ip\":\"" + WiFi.localIP().toString() + "\",";
  jsonResponse += "\"port\":8080,";
  jsonResponse += "\"uptime_ms\":" + String(millis()) + ",";
  jsonResponse += "\"wifi_rssi\":" + String(WiFi.RSSI()) + ",";
  jsonResponse += "\"oled\":true";
  jsonResponse += "}";

  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.send(200, "application/json", jsonResponse);
}

// POST /display -> Receives backend text definitions
void handleDisplay() {
  String bodyText = "";

  if (server.hasArg("plain")) {
    bodyText = server.arg("plain");
  } else if (server.args() > 0) {
    bodyText = server.argName(0);
  }

  if (bodyText.length() == 0) {
    server.sendHeader("Access-Control-Allow-Origin", "*");
    server.send(400, "text/plain", "Bad Request: Empty Body");
    return;
  }

  // Check maximum reasonable payload length (formattedLines capacity is 40 lines * 18 chars ~ 720 chars)
  if (bodyText.length() > 1500) {
    server.sendHeader("Access-Control-Allow-Origin", "*");
    server.send(413, "text/plain", "Payload Too Large: Text exceeds buffer capacity");
    return;
  }

  updateOLED(bodyText);
  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.send(200, "text/plain", "Display updated successfully");
}

void handleNotFound() {
  server.send(404, "text/plain", "Not Found");
}

void setup() {
  Serial.begin(115200);
  delay(500);

  u8g2.begin();
  updateOLED("Ready for Backend!");

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
  Serial.print("📡 IP Address: ");
  Serial.println(WiFi.localIP());
  Serial.print("📶 RSSI: ");
  Serial.println(WiFi.RSSI());

  server.on("/buttons", HTTP_GET, handleButtons);
  server.on("/display", HTTP_POST, handleDisplay);
  server.on("/health", HTTP_GET, handleHealth);
  server.onNotFound(handleNotFound);

  server.begin();
  Serial.println("🚀 DevKit Server listening on Port 8080.");
}

void loop() {
  server.handleClient();
  checkScrollTrigger(); // Hardware check for both buttons pressed simultaneously
}