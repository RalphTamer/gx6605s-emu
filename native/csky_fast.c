/* SPDX-License-Identifier: LGPL-2.0-or-later
 * Copyright (c) 2024 Alibaba Group. All rights reserved.
 * ABIv1 integer fast path. Encoding provenance: docs/references.md.
 * Unsupported instructions, TLB accesses and MMIO return to cpu.py before
 * executing that instruction. No peripheral shortcuts or guest-code patching.
 */
#include <stdint.h>
#include <stddef.h>

typedef struct { uint32_t base, size; uint8_t *data; uint32_t readonly; } RAM;
typedef struct {
    uint32_t r[16], pc, carry, mmu, supervisor, msa0, msa1;
    uint32_t history_pc[32], history_word[32];
    uint32_t memory_writes;
} State;

static uint8_t *memory(State *s, RAM *ram, uint32_t nr, uint32_t a, uint32_t n, int write) {
    if (a % n || (uint64_t)a + n > 0x100000000ULL) return NULL;
    if (s->mmu) {
        if (a < 0x80000000U || a >= 0xc0000000U || !s->supervisor) return NULL;
        uint32_t msa = a < 0xa0000000U ? s->msa0 : s->msa1;
        if (!(msa & 2) || (write && !(msa & 4))) return NULL;
        a = (a & 0x1fffffffU) + (msa & 0xe0000000U);
    }
    for (uint32_t i = 0; i < nr; i++) {
        if (a >= ram[i].base && (uint64_t)a + n <= (uint64_t)ram[i].base + ram[i].size) {
            if (write && ram[i].readonly) return NULL;
            return ram[i].data + (a - ram[i].base);
        }
    }
    return NULL;
}
static uint32_t load(uint8_t *p, uint32_t n) {
    uint32_t value = 0;
    for (uint32_t i = 0; i < n; i++) value |= (uint32_t)p[i] << (i * 8);
    return value;
}
static uint32_t reverse(uint32_t v) {
    v = ((v >> 1) & 0x55555555U) | ((v & 0x55555555U) << 1);
    v = ((v >> 2) & 0x33333333U) | ((v & 0x33333333U) << 2);
    v = ((v >> 4) & 0x0f0f0f0fU) | ((v & 0x0f0f0f0fU) << 4);
    return __builtin_bswap32(v);
}

uint32_t csky_run(State *s, RAM *ram, uint32_t nr, uint32_t limit, const uint32_t *breakpoints, uint32_t nb) {
    uint32_t done = 0, *r = s->r;
    while (done < limit) {
        for (uint32_t i = 0; i < nb; i++) if (s->pc == breakpoints[i]) goto finished;
        uint8_t *p = memory(s, ram, nr, s->pc, 2, 0);
        if (!p) break;
        uint32_t w = load(p, 2), hi = w >> 12, sub = (w >> 8) & 15;
        uint32_t x = w & 15, y = (w >> 4) & 15, imm = (w >> 4) & 31;
        uint32_t next = s->pc + 2, c = s->carry;
        if (w == 1) { /* sync */ }
        else if (w <= 6 || w >> 4 == 1 || w >> 4 == 0x11f) break;
        else if (hi == 0 && sub == 0) {
            switch (y) {
                case 2: r[x] = c; break;
                case 3: r[x] = 1 - c; break;
                case 8: r[x] -= c; break;
                case 9: r[x] -= 1 - c; break;
                case 10: r[x] += c; break;
                case 11: r[x] += 1 - c; break;
                case 12: next = r[x] & ~1U; break;
                case 13: next = r[x] & ~1U; r[15] = s->pc + 2; break;
                case 14: r[x] = r[x] ? (uint32_t)__builtin_clz(r[x]) : 32; break;
                case 15: r[x] = reverse(r[x]); break;
                default: goto finished;
            }
        } else if (hi == 0 && sub == 1) {
            switch (y) {
                case 0: case 1: case 2: case 3:
                    r[1] = (r[x] >> (y * 8)) & 255; c = r[1] != 0; break;
                case 4: r[x] &= 255; break;
                case 5: r[x] = (int8_t)r[x]; break;
                case 6: r[x] &= 65535; break;
                case 7: r[x] = (int16_t)r[x]; break;
                case 8: r[x]--; c = (int32_t)r[x] < 0; break;
                case 9: c = (r[x] & 255) && ((r[x] >> 8) & 255) && ((r[x] >> 16) & 255) && (r[x] >> 24); break;
                case 10: r[x]--; c = (int32_t)r[x] > 0; break;
                case 11: r[x]--; c = r[x] != 0; break;
                case 12: if (c) r[x] = 0; break;
                case 13: if (!c) r[x] = 0; break;
                case 14: if ((int32_t)r[x] < 0) r[x] = 0U - r[x]; break;
                case 15: r[x] = ~r[x]; break;
            }
        } else if (hi == 0) {
            uint64_t value;
            uint32_t shift = r[y] & 63;
            switch (sub) {
                case 2: if (c) r[x] = r[y]; break;
                case 3: r[x] *= r[y]; break;
                case 5: r[x] -= r[y]; break;
                case 6: value = (uint64_t)r[x] + r[y] + c; r[x] = value; c = value >> 32; break;
                case 7: value = (uint64_t)r[y] + (1 - c); c = (uint64_t)r[x] >= value; r[x] -= (uint32_t)value; break;
                case 10: if (!c) r[x] = r[y]; break;
                case 11: r[x] = shift > 31 ? 0 : r[x] >> shift; break;
                case 12: c = r[x] >= r[y]; break;
                case 13: c = (int32_t)r[x] < (int32_t)r[y]; break;
                case 14: c = (r[x] & r[y]) != 0; break;
                case 15: c = r[x] != r[y]; break;
                default: goto finished;
            }
        } else if (hi == 1) {
            uint32_t shift = r[y] & 63;
            switch (sub) {
                case 2: r[x] = r[y]; break;
                case 3: r[x] = (r[y] & 32) ? 0 : 1U << (r[y] & 31); break;
                case 4: r[x] = r[y] - r[x]; break;
                case 5: r[x] += r[y] * 4; break;
                case 6: r[x] &= r[y]; break;
                case 7: r[x] ^= r[y]; break;
                case 10: r[x] = (int32_t)r[x] >> (shift > 31 ? 31 : shift); break;
                case 11: r[x] = shift > 31 ? 0 : r[x] << shift; break;
                case 12: r[x] += r[y]; break;
                case 13: r[x] += r[y] * 2; break;
                case 14: r[x] |= r[y]; break;
                case 15: r[x] &= ~r[y]; break;
                default: goto finished;
            }
        } else if (hi == 2) {
            if (sub == 12 && y == 1) {
                if (!r[1]) break;
                r[x] /= r[1];
            } else if ((sub == 12 && (y == 0 || y >= 8)) || sub == 13) {
                r[x] = imm ? (1U << imm) - 1 : 0xffffffffU;
            } else switch (sub >> 1) {
                case 0: r[x] += imm + 1; break;
                case 1: c = (int32_t)r[x] < (int32_t)(imm + 1); break;
                case 2: r[x] -= imm + 1; break;
                case 4: r[x] = imm - r[x]; break;
                case 5: c = r[x] != imm; break;
                case 7: r[x] &= imm; break;
                default: goto finished;
            }
        } else if (hi == 3) {
            if (sub == 2 && y == 1) {
                if (!r[1]) break;
                r[x] = (uint32_t)((int64_t)(int32_t)r[x] / (int32_t)r[1]);
            } else if (sub <= 1) r[x] &= ~(1U << imm);
            else if (sub == 3 || (sub == 2 && y >= 7)) r[x] = 1U << imm;
            else if (sub == 4 || sub == 5) r[x] |= 1U << imm;
            else if (sub == 6 || sub == 7) c = (r[x] >> imm) & 1;
            else if (sub >= 8) {
                uint32_t old = r[x];
                switch (sub >> 1) {
                    case 4: if (imm) r[x] = (old << imm) | (old >> (32 - imm));
                            else { r[x] = (old >> 1) | (c << 31); c = old & 1; } break;
                    case 5: r[x] = (int32_t)old >> (imm ? imm : 1); if (!imm) c = old & 1; break;
                    case 6: r[x] = old << (imm ? imm : 1); if (!imm) c = old >> 31; break;
                    case 7: r[x] = old >> (imm ? imm : 1); if (!imm) c = old & 1; break;
                }
            } else break;
        } else if (hi == 6 && sub < 8) r[x] = (w >> 4) & 127;
        else if (hi == 6 && sub == 8) r[x] = (int16_t)r[x] * (int16_t)r[y];
        else if (hi == 7) {
            uint32_t literal = (s->pc + 2 + (w & 255) * 4) & ~3U;
            p = memory(s, ram, nr, literal, 4, 0);
            if (!p) break;
            uint32_t value = load(p, 4);
            if (sub == 0 || sub == 15) {
                next = value & ~1U;
                if (sub == 15) r[15] = s->pc + 2;
            } else r[sub] = value;
        } else if (hi >= 8 && hi <= 13) {
            uint32_t n = hi < 10 ? 4 : hi < 12 ? 1 : 2;
            uint32_t address = r[x] + y * n;
            p = memory(s, ram, nr, address, n, hi & 1);
            if (!p) break;
            if (hi & 1) {
                for (uint32_t i = 0; i < n; i++) p[i] = r[sub] >> (8 * i);
                s->memory_writes++;
            } else r[sub] = load(p, n);
        } else if (hi >= 14) {
            int32_t displacement = w & 0x7ff;
            if (displacement & 0x400) displacement -= 0x800;
            uint32_t target = s->pc + 2 + displacement * 2;
            if (hi == 15) {
                if (sub >= 8) r[15] = next;
                next = target;
            } else if ((sub < 8 && c) || (sub >= 8 && !c)) next = target;
        } else break;
        s->history_pc[done & 31] = s->pc;
        s->history_word[done & 31] = w;
        s->pc = next;
        s->carry = c;
        done++;
    }
finished:
    return done;
}
