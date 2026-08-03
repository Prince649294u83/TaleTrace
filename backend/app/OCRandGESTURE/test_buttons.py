import os
import sys
import time
import requests

# Windows consoles default to cp1252 and cannot encode the status emoji below.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# Set your ESP32 DevKit IP address here.
# The DevKit firmware (esp32/esp32.ino) serves the button endpoint on port 8080.
ESP32_BUTTONS_URL = os.environ.get(
    "ESP32_BUTTONS_URL", "http://192.168.1.26:8080/buttons"
)


def test_buttons():
    print("=" * 50)
    print("🔘 ESP32 Button Tester")
    print(f"📡 Polling target: {ESP32_BUTTONS_URL}")
    print("Press Ctrl+C to exit")
    print("=" * 50 + "\n")

    while True:
        try:
            response = requests.get(ESP32_BUTTONS_URL, timeout=2)

            if response.status_code == 200:
                data = response.json()
                btn_momentary = data.get("btn_momentary", False)
                btn_toggle = data.get("btn_toggle", False)

                # Format terminal display string
                status_mom = "🔴 PRESSED" if btn_momentary else "⚪ RELEASED"
                status_tog = "🟢 ON (Meaning)" if btn_toggle else "⚪ OFF"

                print(
                    f"\rMomentary (GPIO 4): {status_mom:<15} | Toggle (GPIO 5): {status_tog:<20}",
                    end="",
                    flush=True,
                )
            else:
                print(f"\n⚠️ HTTP Error {response.status_code}")

        except requests.exceptions.RequestException as e:
            print(
                f"\n❌ Cannot connect to ESP32 ({e}). Check IP address and Wi-Fi connection."
            )

        time.sleep(0.2)  # Check 5 times per second


if __name__ == "__main__":
    try:
        test_buttons()
    except KeyboardInterrupt:
        print("\n\n🛑 Button test stopped.")