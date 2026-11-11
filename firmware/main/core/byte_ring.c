#include "byte_ring.h"

#include <string.h>

void ring_init(byte_ring_t *r, uint8_t *storage, size_t cap)
{
    r->buf = storage;
    r->cap = cap;
    ring_reset(r);
}

void ring_reset(byte_ring_t *r)
{
    r->head = r->tail = r->used = 0;
}

size_t ring_used(const byte_ring_t *r) { return r->used; }
size_t ring_free(const byte_ring_t *r) { return r->cap - r->used; }

size_t ring_write(byte_ring_t *r, const uint8_t *src, size_t len)
{
    size_t n = len < ring_free(r) ? len : ring_free(r);
    size_t first = r->cap - r->head;
    if (first > n) first = n;
    memcpy(r->buf + r->head, src, first);
    memcpy(r->buf, src + first, n - first);
    r->head = (r->head + n) % r->cap;
    r->used += n;
    return n;
}

size_t ring_read(byte_ring_t *r, uint8_t *dst, size_t len)
{
    size_t n = len < r->used ? len : r->used;
    size_t first = r->cap - r->tail;
    if (first > n) first = n;
    memcpy(dst, r->buf + r->tail, first);
    memcpy(dst + first, r->buf, n - first);
    r->tail = (r->tail + n) % r->cap;
    r->used -= n;
    return n;
}
