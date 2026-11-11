// Bounded single-producer / single-consumer byte ring buffer (no locking here;
// the firmware wraps calls in a mutex so reset is always safe).
#pragma once

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    uint8_t *buf;
    size_t cap;
    size_t head;   // write index
    size_t tail;   // read index
    size_t used;
} byte_ring_t;

void ring_init(byte_ring_t *r, uint8_t *storage, size_t cap);
void ring_reset(byte_ring_t *r);
size_t ring_used(const byte_ring_t *r);
size_t ring_free(const byte_ring_t *r);
// Writes up to len bytes; returns bytes written (never overwrites unread data).
size_t ring_write(byte_ring_t *r, const uint8_t *src, size_t len);
// Reads up to len bytes; returns bytes read.
size_t ring_read(byte_ring_t *r, uint8_t *dst, size_t len);

#ifdef __cplusplus
}
#endif
