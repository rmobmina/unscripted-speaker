// Backend client (HTTP/1.1 over TLS when the URL is https://).
//
// Transport (documented in docs/ARCHITECTURE.md):
//   POST   /api/v1/device/turns              raw PCM s16le 16 kHz mono -> JSON reply metadata
//   GET    /api/v1/device/turns/{id}/audio   chunked PCM s16le 16 kHz mono (streamed)
//   DELETE /api/v1/device/turns/{id}         cancel (best effort)
//   GET    /api/v1/device/config             runtime settings (ETag, polled every 1 s)
//   POST   /api/v1/device/events             research telemetry batch
//   POST   /api/v1/device/diagnostics        boot diagnostics
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

void backend_start(void);
// Copies the PCM into a per-turn PSRAM buffer and starts an upload worker.
// Returns false if no worker slot / memory is available (bounded).
bool backend_submit_turn(uint32_t turn, const int16_t *pcm, size_t bytes, uint32_t dur_ms, int quality);
void backend_cancel_turn(uint32_t turn);
void backend_mark_followup(uint32_t turn, bool followup);
void backend_net_changed(void);   // re-evaluate online/offline (Wi-Fi edge)
bool backend_online(void);
void backend_set_boot_diag(const char *json);   // sent once the backend is reachable
