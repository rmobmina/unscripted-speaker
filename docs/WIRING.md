# Wiring

**No external wiring is required.** The Waveshare ESP32-S3-AUDIO-Board integrates the microphones, speaker codec/amplifier, RGB LEDs and buttons. Connect it to the Mac with a USB-C data cable; that cable carries power, flashing and the serial console.

Speaker: plug a speaker into the board's speaker connector. Its exact power rating is not documented here. Use the speaker that worked with the earlier `spotify_speaker` firmware.

## Optional: external 16-LED ring (protocol prototype)
Not fitted and not tested. If one is added later, its data pin, power source and level shifting must come from that ring's own datasheet and the board schematic. Do not guess them. Afterwards update `firmware/config/pins.h` (`PIN_LED_DATA`, `LED_COUNT`) and record the evidence there.
