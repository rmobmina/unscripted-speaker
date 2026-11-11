#include "vad.h"

#include <math.h>

void vad_default_config(vad_config_t *c)
{
    c->abs_threshold_dbfs = -48.0f;
    c->snr_db = 9.0f;
    c->start_ms = 100;
    c->frame_ms = 20;
}

void vad_reset(vad_t *v, const vad_config_t *cfg)
{
    *v = (vad_t){0};
    v->cfg = *cfg;
    v->noise_floor_dbfs = -60.0f;
}

float vad_frame_dbfs(const int16_t *pcm, size_t n)
{
    if (n == 0) return -120.0f;
    double acc = 0;
    for (size_t i = 0; i < n; i++) acc += (double)pcm[i] * pcm[i];
    double rms = sqrt(acc / (double)n);
    if (rms < 1e-9) return -120.0f;
    return (float)(20.0 * log10(rms / 32768.0));
}

bool vad_process(vad_t *v, const int16_t *pcm, size_t n, vad_frame_t *out)
{
    bool all_zero = true;
    for (size_t i = 0; i < n; i++) {
        int32_t s = pcm[i];
        int32_t a = s < 0 ? -s : s;
        if (a > v->peak) v->peak = a;
        if (a >= 32700) v->clipped_samples++;
        if (s != 0) all_zero = false;
        v->energy_sum += (double)s * s;
    }
    v->sample_count += (uint32_t)n;
    v->frames++;
    if (all_zero) v->zero_frames++;

    float db = vad_frame_dbfs(pcm, n);
    bool loud = db > v->cfg.abs_threshold_dbfs && db > v->noise_floor_dbfs + v->cfg.snr_db;

    // Track the noise floor: follow quickly downwards, slowly upwards, and
    // never adapt on frames classified as speech.
    if (!loud) {
        float alpha = db < v->noise_floor_dbfs ? 0.30f : 0.05f;
        v->noise_floor_dbfs += alpha * (db - v->noise_floor_dbfs);
        if (v->noise_floor_dbfs < -90.f) v->noise_floor_dbfs = -90.f;
    }

    bool started = false;
    if (loud) {
        v->speech_run_ms += v->cfg.frame_ms;
        v->silence_run_ms = 0;
        if (!v->speech_detected && v->speech_run_ms >= v->cfg.start_ms) {
            v->speech_detected = true;
            started = true;
        }
    } else {
        v->speech_run_ms = 0;
        v->silence_run_ms += v->cfg.frame_ms;
    }
    if (out) *out = loud ? VAD_FRAME_SPEECH : VAD_FRAME_SILENCE;
    return started;
}

float vad_rms_dbfs(const vad_t *v)
{
    if (v->sample_count == 0 || v->energy_sum <= 0) return -120.0f;
    double rms = sqrt(v->energy_sum / v->sample_count);
    return (float)(20.0 * log10(rms / 32768.0));
}

capture_quality_t vad_quality(const vad_t *v)
{
    if (v->frames > 0 && v->zero_frames == v->frames) return CAPTURE_NO_SIGNAL;
    if (v->sample_count > 0 && v->clipped_samples * 100u > v->sample_count) return CAPTURE_CLIPPED;
    if (!v->speech_detected) return CAPTURE_SILENT;
    return CAPTURE_OK;
}

const char *capture_quality_name(capture_quality_t q)
{
    switch (q) {
    case CAPTURE_OK: return "ok";
    case CAPTURE_NO_SIGNAL: return "no_signal";
    case CAPTURE_SILENT: return "silent";
    case CAPTURE_CLIPPED: return "clipped";
    }
    return "?";
}
