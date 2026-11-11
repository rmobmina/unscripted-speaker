// Audio task: microphone capture (push-to-talk, never ambient), streamed reply
// playback, earcons and short spoken system messages. Runs on core 1.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "core/sm.h"

typedef enum {
    CUE_LISTEN = 0,
    CUE_CANCEL,
    CUE_OFFLINE,
} audio_cue_t;

typedef struct {
    // last capture
    uint32_t capture_ms;
    float capture_rms_dbfs;
    int32_t capture_peak;
    uint32_t capture_clipped;
    int capture_quality;          // capture_quality_t
    // playback
    uint32_t underruns;
    uint32_t playback_bytes;
    // boot self-test
    float selftest_rms_left;
    float selftest_rms_right;
    int selftest_zero_frames;
    int selftest_frames;
    bool speaker_ok;
    bool mic_ok;
} audio_stats_t;

void audio_init(bool speaker_ok, bool mic_ok);
void audio_mic_selftest(void);         // call once before audio_start()
void audio_start(void);

// Commands from the controller (non-blocking).
void audio_capture_start(uint32_t turn);
void audio_capture_discard(void);
void audio_capture_submit(uint32_t turn);
void audio_play_cue(audio_cue_t cue);
void audio_play_error(sm_error_t err);   // two-note tone + spoken explanation; posts ERROR_CUE_DONE
void audio_stream_play(uint32_t turn);
void audio_stop_playback(void);

// Playback channel used by the network worker that downloads reply audio.
void playback_begin(uint32_t turn);
// Returns bytes accepted, or -1 if this turn is no longer the playback turn.
int playback_push(uint32_t turn, const uint8_t *data, size_t len, uint32_t timeout_ms);
void playback_finish(uint32_t turn, bool failed);

void audio_get_stats(audio_stats_t *out);
