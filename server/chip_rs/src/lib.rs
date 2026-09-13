//! chip_rs — companion Rust acceleration core for chip_web.py.
//!
//! Owns two genuinely compute-heavy hot paths (the C++ core owns cache,
//! sessions and SSE framing):
//!   * a dependency-free SHA-256 (used for TTS ETag / If-None-Match dedup)
//!   * a cheap but bounded token estimator used to keep the chat context
//!     within a token budget by trimming the oldest non-system messages.
//!
//! C ABI is zero-allocation on the Rust side: callers provide output buffers
//! and we write into them; nothing needs freeing.

use std::os::raw::{c_char, c_int};
use std::slice;

const VERSION: &str = "chip_rs 0.1.0 (std-only: sha256 + token budget)";

// ---------------------------------------------------------------------------
// SHA-256 (FIPS 180-4, std-only)
// ---------------------------------------------------------------------------

const K: [u32; 64] = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
];

fn sha256(data: &[u8]) -> [u8; 32] {
    let mut h: [u32; 8] = [
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
    ];

    let mut msg = data.to_vec();
    let ml = msg.len();
    let klen = (56usize.wrapping_sub((ml + 1) % 64) + 64) % 64;
    msg.push(0x80);
    msg.extend(std::iter::repeat(0u8).take(klen));
    msg.extend_from_slice(&((ml as u64).wrapping_mul(8)).to_be_bytes());

    let mut w = [0u32; 64];
    for chunk in msg.chunks_exact(64) {
        for i in 0..16 {
            w[i] = u32::from_be_bytes([chunk[i * 4], chunk[i * 4 + 1], chunk[i * 4 + 2], chunk[i * 4 + 3]]);
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16]
                .wrapping_add(s0)
                .wrapping_add(w[i - 7])
                .wrapping_add(s1);
        }
        let (mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut hh) =
            (h[0], h[1], h[2], h[3], h[4], h[5], h[6], h[7]);
        for i in 0..64 {
            let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let ch = (e & f) ^ (!e & g);
            let t1 = hh
                .wrapping_add(s1)
                .wrapping_add(ch)
                .wrapping_add(K[i])
                .wrapping_add(w[i]);
            let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let maj = (a & b) ^ (a & c) ^ (b & c);
            let t2 = s0.wrapping_add(maj);
            hh = g;
            g = f;
            f = e;
            e = d.wrapping_add(t1);
            d = c;
            c = b;
            b = a;
            a = t1.wrapping_add(t2);
        }
        h[0] = h[0].wrapping_add(a);
        h[1] = h[1].wrapping_add(b);
        h[2] = h[2].wrapping_add(c);
        h[3] = h[3].wrapping_add(d);
        h[4] = h[4].wrapping_add(e);
        h[5] = h[5].wrapping_add(f);
        h[6] = h[6].wrapping_add(g);
        h[7] = h[7].wrapping_add(hh);
    }

    let mut out = [0u8; 32];
    for i in 0..8 {
        out[i * 4..i * 4 + 4].copy_from_slice(&h[i].to_be_bytes());
    }
    out
}

fn hex(digest: &[u8; 32], out: &mut [u8]) {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut n = 0usize;
    for b in digest {
        out[n] = HEX[((b >> 4) & 0x0f) as usize];
        out[n + 1] = HEX[(b & 0x0f) as usize];
        n += 2;
    }
}

// ---------------------------------------------------------------------------
// Token estimation (heuristic, bounded cost)
// ---------------------------------------------------------------------------

fn estimate_tokens(s: &str) -> u64 {
    if s.is_empty() {
        return 0;
    }
    let mut ascii = 0u64;
    let mut wide = 0u64;
    for ch in s.chars() {
        if (ch as u32) < 128 {
            ascii += 1;
        } else {
            wide += 1;
        }
    }
    let t = (ascii + 3) / 4 + wide; // ~4 asc chars/token, 1 wide char/token
    t.max(1)
}

// ---------------------------------------------------------------------------
// Tolerant minimal JSON reader (array-of-objects with "content" strings)
// ---------------------------------------------------------------------------

fn skip_ws(b: &[u8], i: &mut usize) {
    while *i < b.len() && (b[*i] == b' ' || b[*i] == b'\n' || b[*i] == b'\t' || b[*i] == b'\r') {
        *i += 1;
    }
}

/// Reads a quoted JSON string (escapes are collapsed to their byte value;
/// \uXXXX becomes a literal U+FFFD marker). Returns None if malformed.
fn read_string(b: &[u8], i: &mut usize) -> Option<String> {
    if b.get(*i) != Some(&b'"') {
        return None;
    }
    *i += 1;
    let mut out: Vec<u8> = Vec::new();
    while *i < b.len() {
        let c = b[*i];
        if c == b'"' {
            *i += 1;
            return Some(String::from_utf8_lossy(&out).into_owned());
        }
        if c == b'\\' {
            if *i + 1 >= b.len() {
                return None;
            }
            let e = b[*i + 1];
            if e == b'u' && *i + 6 <= b.len() {
                // \uXXXX → single U+FFFD (keeps escaped & raw CJK scoring equal)
                *i += 6;
                out.extend_from_slice(&[0xEF, 0xBF, 0xBD]);
            } else {
                *i += 2;
                match e {
                    b'"' => out.push(b'"'),
                    b'\\' => out.push(b'\\'),
                    b'/' => out.push(b'/'),
                    b'n' => out.push(b'\n'),
                    b't' => out.push(b'\t'),
                    b'r' => out.push(b'\r'),
                    b'b' => out.push(0x08),
                    b'f' => out.push(0x0c),
                    _ => out.push(b'?'),
                }
            }
        } else {
            out.push(c);
            *i += 1;
        }
    }
    None
}

fn skip_value(b: &[u8], i: &mut usize) {
    skip_ws(b, i);
    match b.get(*i).copied() {
        Some(b'"') => {
            let _ = read_string(b, i);
        }
        Some(b'{') => skip_object(b, i),
        Some(b'[') => skip_array(b, i),
        Some(_) => {
            while *i < b.len() && b[*i] != b',' && b[*i] != b']' && b[*i] != b'}' {
                *i += 1;
            }
        }
        None => {}
    }
}

fn skip_object(b: &[u8], i: &mut usize) {
    if b.get(*i) != Some(&b'{') {
        return;
    }
    *i += 1;
    loop {
        skip_ws(b, i);
        match b.get(*i).copied() {
            Some(b'}') => {
                *i += 1;
                return;
            }
            Some(b',') => *i += 1,
            Some(_) => {
                let key = read_string(b, i);
                skip_ws(b, i);
                if b.get(*i) == Some(&b':') {
                    *i += 1;
                    skip_value(b, i);
                }
                let _ = key;
            }
            None => return,
        }
    }
}

fn skip_array(b: &[u8], i: &mut usize) {
    if b.get(*i) != Some(&b'[') {
        return;
    }
    *i += 1;
    loop {
        skip_ws(b, i);
        match b.get(*i).copied() {
            Some(b']') => {
                *i += 1;
                return;
            }
            Some(b',') => *i += 1,
            Some(_) => skip_value(b, i),
            None => return,
        }
    }
}

/// Token count of a single {role, content, ...} run; counts only "content".
fn message_tokens(b: &[u8], i: &mut usize) -> u64 {
    debug_assert_eq!(b.get(*i), Some(&b'{'));
    *i += 1;
    let mut tokens = 0u64;
    loop {
        skip_ws(b, i);
        match b.get(*i).copied() {
            Some(b'}') => {
                *i += 1;
                return tokens;
            }
            Some(b',') => *i += 1,
            Some(_) => {
                let key = read_string(b, i);
                skip_ws(b, i);
                if b.get(*i) == Some(&b':') {
                    *i += 1;
                    skip_ws(b, i);
                    if key.as_deref() == Some("content") && b.get(*i) == Some(&b'"') {
                        if let Some(v) = read_string(b, i) {
                            tokens += estimate_tokens(&v);
                        }
                    } else {
                        skip_value(b, i);
                    }
                }
            }
            None => return tokens,
        }
    }
}

fn messages_tokens_json(b: &[u8], out: &mut [u64]) -> c_int {
    let mut i = 0usize;
    skip_ws(b, &mut i);
    if b.get(i) != Some(&b'[') {
        return -1;
    }
    let mut tokens: Vec<u64> = Vec::new();
    i += 1;
    loop {
        skip_ws(b, &mut i);
        match b.get(i).copied() {
            Some(b']') => break,
            Some(b',') => i += 1,
            Some(b'{') => tokens.push(message_tokens(b, &mut i)),
            Some(_) => skip_value(b, &mut i),
            None => return -1,
        }
    }
    if out.len() == 0 {
        return -1;
    }
    let n = (tokens.len() + 1).min(out.len());
    out[0] = tokens.len() as u64;
    for (slot, v) in out[1..n].iter_mut().zip(tokens.iter().take(n - 1)) {
        *slot = *v;
    }
    (n - 1) as c_int
}

// ---------------------------------------------------------------------------
// C ABI
// ---------------------------------------------------------------------------

/// Estimate tokens for one NUL-terminated string.
#[no_mangle]
pub extern "C" fn rs_token_estimate(text: *const c_char) -> u64 {
    if text.is_null() {
        return 0;
    }
    let cstr = unsafe { std::ffi::CStr::from_ptr(text) };
    match cstr.to_str() {
        Ok(s) => estimate_tokens(s),
        Err(_) => 0,
    }
}

/// Score every "content" in a JSON messages array. Writes `[count, t0,
/// t1, ...]` into `out` (capacity = count+1 entries), returns number of
/// messages scored, or -1 on malformed input.
#[no_mangle]
pub extern "C" fn rs_messages_tokens(
    data: *const u8,
    len: usize,
    out: *mut u64,
    out_cap: u64,
) -> c_int {
    if data.is_null() {
        return -1;
    }
    let buf = unsafe { slice::from_raw_parts(data, len) };
    if out.is_null() {
        return 0;
    }
    let cap = (out_cap as usize).min(isize::MAX as usize / 8);
    if cap == 0 {
        return 0;
    }
    let o = unsafe { slice::from_raw_parts_mut(out, cap) };
    messages_tokens_json(buf, o)
}

/// SHA-256 hex digest of `data`. `out` must be at least 65 bytes; writes 64
/// hex chars + NUL. Returns 0 on success, -1 on bad output buffer.
#[no_mangle]
pub extern "C" fn rs_sha256_hex(data: *const u8, len: usize, out: *mut c_char) -> c_int {
    let buf = if data.is_null() && len != 0 {
        return -1;
    } else if data.is_null() {
        &[][..]
    } else {
        unsafe { slice::from_raw_parts(data, len) }
    };
    if out.is_null() {
        return -1;
    }
    let o = unsafe { slice::from_raw_parts_mut(out as *mut u8, 65usize) };
    let digest = sha256(buf);
    hex(&digest, &mut o[..64]);
    o[64] = 0;
    0
}

/// Fill `out` (cap bytes) with an identifier; NUL-terminated. Returns bytes
/// written (excluding NUL), or -1 if the buffer is too small.
#[no_mangle]
pub extern "C" fn rs_version(out: *mut c_char, cap: usize) -> c_int {
    if out.is_null() || cap == 0 {
        return -1;
    }
    let bytes = VERSION.as_bytes();
    let n = bytes.len().min(cap - 1);
    let o = unsafe { slice::from_raw_parts_mut(out as *mut u8, cap) };
    o[..n].copy_from_slice(&bytes[..n]);
    o[n] = 0;
    n as c_int
}