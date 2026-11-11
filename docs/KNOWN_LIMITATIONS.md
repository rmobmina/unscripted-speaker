# Known limitations

- **Not a medical device or alert system.** Safety flags are logged for researchers only. No caregiver notification is sent, and none is implemented.
- **The button GPIO, the audible speaker and the LED colour order/orientation are unverified** (see HARDWARE_AUDIT.md).
- **7 on-board LEDs**, not the 16-LED ring from the protocol.
- **HTTP on the local network** with bearer tokens. This is acceptable for a trusted lab network only; use HTTPS for anything else.
- **Wi-Fi credentials are compiled into the firmware.** There is no on-device Wi-Fi setup. Change networks by re-running `configure_device.sh` and flashing again.
- **The backend URL is the Mac's IP at configure time.** A DHCP change breaks the connection until the device is reconfigured.
- **Offline clips use a placeholder macOS voice** until regenerated with `--engine openai`.
- **TTS models shut down on 2027-01-06** (see RESEARCH_REPRODUCIBILITY.md).
- **The deterministic safety screen is English-only and keyword-based.** Indirect wordings rely on the model. Human review of live eval runs is required.
- **Dashboard microphone recording** needs a secure context (open the dashboard at `http://localhost:8000` on the Mac). From other machines, upload a WAV file instead.
- **Energy-based VAD** may struggle with TV or background speech. It is tunable from the Research tab.
- **The condition latency metric** includes up to one poll interval (1 s).
