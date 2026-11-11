# Hardware audit: what is verified and what is assumed

Board: **Waveshare ESP32-S3-AUDIO-Board**. Identified from the pins the existing firmware in this repo uses, then cross-checked against the Waveshare wiki. The schematic link on the wiki returned 404 at audit time.

## Verified on the physical board (11 Nov 2026, firmware 2.0.0-dev.1)

| Item | Evidence |
|---|---|
| ESP32-S3 (QFN56) rev v0.2, 16 MB flash | esptool chip read |
| 8 MB octal PSRAM works | boot log: `esp_psram: Found 8MB PSRAM device`, `SPI SRAM memory test OK` |
| I2C bus on SDA 11 / SCL 10 | boot I2C scan found 0x18, 0x20, 0x40, 0x51 |
| ES8311 speaker codec at 0x18 initialises | `Open codec device OK`, diagnostics `es8311:true` |
| ES7210 mic ADC at 0x40 initialises, MIC1+MIC2 | diagnostics `es7210:true` |
| TCA9555 expander at 0x20 responds | diagnostics `tca9555:true` |
| Microphone data path (I2S DIN GPIO15) delivers non-zero samples | mic self-test: L −63.8 dBFS, R −64.1 dBFS, 0/20 all-zero frames (quiet room) |
| Wi-Fi (2.4 GHz) connects; same subnet as the Mac | device IP 192.168.2.30, Mac 192.168.2.18, RSSI −37 to −40 dBm |
| Device ↔ backend HTTP: config poll, telemetry, diagnostics upload | backend shows `dev-01` connected, `client_kind=device`, events labelled REAL_HARDWARE |
| Condition change reaches the device | 9/9 changes acknowledged; see TESTING.md |

## NOT verified (needs a person at the board)

| Item | Assumption in firmware | How to verify |
|---|---|---|
| **Main button GPIO** | GPIO0 (BOOT key), active low (`config/pins.h`, tagged `[VERIFY]`) | Press BOOT: serial shows `button` events and the ring turns to LISTENING. The firmware also logs any TCA9555 input change for the first 120 s after boot, which reveals buttons wired to the expander (K1–K3). |
| **Audible speaker output** | Amp enable = TCA9555 pin 8 (from prior firmware) | `speaker_ok` only means the codec initialised. Listen for the boot chime and the listening cue. |
| **LED ring: data pin, count, colour order, orientation** | GPIO38, 7 LEDs, RGB order (from prior code) | Dashboard → Research → LED calibration: "Show top LED" and "Colour order test" (LEDs 0/1/2 should be red/green/blue). Then set offset / reverse / GRB. |
| Speech capture quality at 40 cm | 30 dB mic gain, energy VAD at −48 dBFS / 9 dB SNR | Loopback test (Research tab), speaking from 40 cm. |
| I2C device 0x51 | Not used. Probably the board's RTC; undocumented | None needed |
| 16-LED ring from the protocol | **Not present**: the board has 7 LEDs | Firmware supports up to 32; set `LED_COUNT` in `config/pins.h` if an external ring is fitted (wiring in WIRING.md) |

Do not run participant sessions until every row in this table has been checked.
