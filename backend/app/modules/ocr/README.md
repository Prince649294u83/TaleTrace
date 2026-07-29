# Milestone 1 OCR Testing

This milestone accepts one JPEG at a time, preprocesses it with OpenCV, sends it
to Google Cloud Vision `DOCUMENT_TEXT_DETECTION`, and writes the result to
`backend/output/output.txt`.

## 1. Install and authenticate

From the repository root on Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

In Google Cloud, select or create a project, enable **Cloud Vision API**, and
enable billing. Then choose one authentication method:

```powershell
# Recommended for local development (requires the Google Cloud CLI)
gcloud auth application-default login
gcloud auth application-default set-quota-project YOUR_PROJECT_ID
```

Alternatively, create a service account with permission to use Cloud Vision,
download its JSON key outside this repository, and set:

```powershell
$env:GOOGLE_APPLICATION_CREDENTIALS="C:\secure\taletrace-vision.json"
```

The JSON file is the service-account key. Do not copy it into TaleTrace or
commit it. A raw `GOOGLE_CLOUD_VISION_API_KEY` string is not used by this
backend; the Google SDK authenticates through Application Default Credentials.

## 2. Start and test the backend

Run one worker so only one OCR transaction executes at a time:

```powershell
$env:DEBUG="false"
uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

Check health and upload one JPEG:

```powershell
curl.exe http://127.0.0.1:8000/health
curl.exe -X POST http://127.0.0.1:8000/upload -F "image=@sample.jpg;type=image/jpeg"
```

Postman uses the same request: `POST /upload`, **Body > form-data**, key
`image`, type **File**, and one JPEG. Do not manually add the multipart
`Content-Type` header because Postman must generate its boundary.

A successful request returns:

```json
{
  "status": "success",
  "processing": "completed",
  "characters": 1234,
  "output_file": "backend/output/output.txt"
}
```

Inspect `backend/output/latest.jpg`, `backend/output/processed.jpg`, and
`backend/output/output.txt`. Each successful request overwrites these files.

## 3. Send one ESP32-CAM frame

Connect the ESP32-CAM and backend computer to the same network. Start Uvicorn
with `--host 0.0.0.0`, allow TCP port `8000` through the computer firewall, and
replace `192.168.1.100` below with the computer's LAN IPv4 address. After the
camera and Wi-Fi are initialized, call this function once:

```cpp
#include <HTTPClient.h>
#include "esp_camera.h"

void uploadOneFrame() {
  camera_fb_t *frame = esp_camera_fb_get();
  if (!frame || frame->format != PIXFORMAT_JPEG) return;

  const String boundary = "TaleTraceBoundary";
  const String head =
      "--" + boundary + "\r\n"
      "Content-Disposition: form-data; name=\"image\"; filename=\"capture.jpg\"\r\n"
      "Content-Type: image/jpeg\r\n\r\n";
  const String tail = "\r\n--" + boundary + "--\r\n";
  const size_t length = head.length() + frame->len + tail.length();
  uint8_t *body = (uint8_t *)malloc(length);

  if (body) {
    memcpy(body, head.c_str(), head.length());
    memcpy(body + head.length(), frame->buf, frame->len);
    memcpy(body + head.length() + frame->len, tail.c_str(), tail.length());

    HTTPClient http;
    http.begin("http://192.168.1.100:8000/upload");
    http.addHeader("Content-Type", "multipart/form-data; boundary=" + boundary);
    int status = http.POST(body, length);
    Serial.printf("HTTP %d\n%s\n", status, http.getString().c_str());
    http.end();
    free(body);
  }

  esp_camera_fb_return(frame);
}
```

Configure the camera for JPEG output and at least `320x240`. This function does
not loop, stream, queue, or capture continuously. Call it again only when the
previous HTTP request has completed and another test image is needed.

Expected backend logs progress from `Request received` through `TXT written`,
`Processing finished`, and `Execution time`. HTTP `503` normally means Google
credentials are missing; `504` indicates a Vision timeout; `502` indicates a
Vision API failure.
