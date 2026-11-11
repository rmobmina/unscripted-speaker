// Energy-based voice activity detection + capture quality statistics.
// Operates on 16-bit mono frames (20 ms = 320 samples at 16 kHz).
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    float abs_threshold_dbfs;  // frames quieter than this are never speech
    float snr_db;              // speech must exceed the noise floor by this much
    uint16_t start_ms;         // consecutive speech needed to declare speech
    uint16_t frame_ms;
} vad_config_t;

typedef struct {
    vad_config_t cfg;
    float noise_floor_dbfs;
    uint32_t speech_run_ms;
    uint32_t silence_run_ms;     // silence since the last speech frame
    bool speech_detected;        // speech seen at least once in this capture
    // capture quality
    uint32_t frames;
    uint32_t zero_frames;        // frames that are exactly zero (data path fault)
    uint32_t clipped_samples;
    int32_t peak;
    double energy_sum;           // for whole-capture RMS
    uint32_t sample_count;
} vad_t;

typedef enum {
    VAD_FRAME_SILENCE = 0,
    VAD_FRAME_SPEECH = 1,
} vad_frame_t;

typedef enum {
    CAPTURE_OK = 0,
    CAPTURE_NO_SIGNAL,    // all-zero data: microphone/I2S path not delivering audio
    CAPTURE_SILENT,       // signal present but no speech detected
    CAPTURE_CLIPPED,      // > 1% of samples at full scale
} capture_quality_t;

void vad_default_config(vad_config_t *c);
void vad_reset(vad_t *v, const vad_config_t *cfg);
// Returns true exactly once: on the frame where speech is first declared.
bool vad_process(vad_t *v, const int16_t *pcm, size_t n, vad_frame_t *out_frame);
float vad_frame_dbfs(const int16_t *pcm, size_t n);
capture_quality_t vad_quality(const vad_t *v);
float vad_rms_dbfs(const vad_t *v);
const char *capture_quality_name(capture_quality_t q);

#ifdef __cplusplus
}
#endif
