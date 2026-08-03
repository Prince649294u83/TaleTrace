#include "esp_camera.h"
#include <WiFi.h>
#include <ArduinoOTA.h>
#include "soc/soc.h"
#include "soc/rtc_cntl_reg.h"
#include "esp_http_server.h"

// =================== YOUR WIFI CREDENTIALS ===================
const char* ssid = "Smatt";
const char* password = "8394229388583201";

// =================== FIXED STATIC IP CONFIGURATION ===================
IPAddress local_IP(192, 168, 1, 200); 
IPAddress gateway(192, 168, 1, 1);    
IPAddress subnet(255, 255, 255, 0);
IPAddress primaryDNS(8, 8, 8, 8);     

// =================== AI THINKER PINOUT ===================
#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27
#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5
#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22

httpd_handle_t server_httpd = NULL;

// =================== SINGLE FRAME SNAPSHOT ENDPOINT ===================
static esp_err_t capture_handler(httpd_req_t *req) {
    camera_fb_t * fb = esp_camera_fb_get();
    if (!fb) {
        Serial.println("Capture failed!");
        httpd_resp_send_500(req);
        return ESP_FAIL;
    }

    httpd_resp_set_type(req, "image/jpeg");
    httpd_resp_set_hdr(req, "Content-Disposition", "inline; filename=capture.jpg");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");

    esp_err_t res = httpd_resp_send(req, (const char *)fb->buf, fb->len);
    esp_camera_fb_return(fb); // Release buffer memory immediately
    return res;
}

void startServer() {
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.server_port = 80;

    httpd_uri_t capture_uri = {
        .uri      = "/capture",
        .method   = HTTP_GET,
        .handler  = capture_handler,
        .user_ctx = NULL
    };

    if (httpd_start(&server_httpd, &config) == ESP_OK) {
        httpd_register_uri_handler(server_httpd, &capture_uri);
        Serial.println("Server started on port 80. Endpoint: /capture");
    }
}

// =================== SETUP ===================
void setup() {
    // Disable Brownout Detector immediately to prevent power-spike resets
    #ifdef RTC_CNTL_BROWN_OUT_REG
        WRITE_PERI_REG(RTC_CNTL_BROWN_OUT_REG, 0);
    #else
        WRITE_PERI_REG(RTC_CNTL_BROWNOUT_REG, 0);
    #endif

    Serial.begin(115200);
    delay(500); 

    camera_config_t config;
    config.ledc_channel = LEDC_CHANNEL_0;
    config.ledc_timer = LEDC_TIMER_0;
    config.pin_d0 = Y2_GPIO_NUM;
    config.pin_d1 = Y3_GPIO_NUM;
    config.pin_d2 = Y4_GPIO_NUM;
    config.pin_d3 = Y5_GPIO_NUM;
    config.pin_d4 = Y6_GPIO_NUM;
    config.pin_d5 = Y7_GPIO_NUM;
    config.pin_d6 = Y8_GPIO_NUM;
    config.pin_d7 = Y9_GPIO_NUM;
    config.pin_xclk = XCLK_GPIO_NUM;
    config.pin_pclk = PCLK_GPIO_NUM;
    config.pin_vsync = VSYNC_GPIO_NUM;
    config.pin_href = HREF_GPIO_NUM;
    config.pin_sscb_sda = SIOD_GPIO_NUM;
    config.pin_sscb_scl = SIOC_GPIO_NUM;
    config.pin_pwdn = PWDN_GPIO_NUM;
    config.pin_reset = RESET_GPIO_NUM;
    config.xclk_freq_hz = 20000000;
    config.pixel_format = PIXFORMAT_JPEG;

    if (psramFound()) {
        config.frame_size = FRAMESIZE_XGA; // 1024x768 - High resolution for OCR
        config.jpeg_quality = 8;           // Low value = High clarity & low noise
        config.fb_count = 1;               // Maximum memory stability
    } else {
        config.frame_size = FRAMESIZE_SVGA; // 800x600 fallback
        config.jpeg_quality = 10;
        config.fb_count = 1;
    }

    esp_err_t err = esp_camera_init(&config);
    if (err != ESP_OK) {
        Serial.printf("Camera init failed: 0x%x\n", err);
        return;
    }

    sensor_t * s = esp_camera_sensor_get();
    if (s != NULL) {
        s->set_hmirror(s, 0);
        s->set_vflip(s, 1);
        
        // --- COLOR & IMAGE TUNING ---
        s->set_special_effect(s, 0);  // 0 = Full Color (Normal mode)
        s->set_whitebal(s, 1);        // Enable Auto White Balance (AWB) for natural skin tones
        s->set_awb_gain(s, 1);        // Enable AWB gain engine
        s->set_wb_mode(s, 0);         // Auto White Balance Mode
        
        s->set_contrast(s, 1);         // Balanced contrast for gesture recognition & text
        s->set_sharpness(s, 1);        // Enhanced character and skin edge definition
        
        // --- GLARE & OVEREXPOSURE FIXES ---
        s->set_brightness(s, 0);      // Neutral brightness
        s->set_ae_level(s, -1);       // Slightly lower exposure target to avoid harsh glare
        s->set_exposure_ctrl(s, 1);   // Auto Exposure enabled
        s->set_gain_ctrl(s, 1);       // Auto Gain enabled
        s->set_gainceiling(s, GAINCEILING_2X); // Clamp gain ceiling to eliminate noise
    }

    // Configure Static IP
    if (!WiFi.config(local_IP, gateway, subnet, primaryDNS)) {
        Serial.println("Static IP Config Failed!");
    }

    WiFi.begin(ssid, password);
    while (WiFi.status() != WL_CONNECTED) {
        delay(500);
        Serial.print(".");
    }

    Serial.println("\nWiFi Connected!");
    Serial.print("ESP32-CAM Endpoint Ready: http://");
    Serial.println(WiFi.localIP());

    ArduinoOTA.setHostname("esp32-cam-stream");
    ArduinoOTA.begin();

    startServer();
}

// =================== MAIN LOOP ===================
void loop() {
    ArduinoOTA.handle();
    delay(10);
}