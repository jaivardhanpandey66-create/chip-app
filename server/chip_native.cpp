// chip_native.cpp — native acceleration core for CHIP's web backend.
//
// Exposes a C ABI (callable from Python via ctypes) for the hot paths:
//   • a thread-safe, TTL-bounded LRU byte cache       (TTS audio, sessions)
//   • streaming-SSE frame builder
//   • JSON-args truncation for the tool log
//
// Build:  g++ -O2 -std=c++17 -fPIC -shared -pthread chip_native.cpp -o libchip_native.so
//
// No third-party dependencies — only the C++ standard library.

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <list>
#include <mutex>
#include <string>
#include <unordered_map>

namespace {

using Clock = std::chrono::steady_clock;
using TimePoint = Clock::time_point;

inline long long now_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now().time_since_epoch()).count();
}

struct Entry {
    std::string data;
    long long born_ms;
    long long ttl_ms;  // <=0 means immortal
};

// Thread-safe LRU byte store with item+byte budgets and optional TTL.
struct Lru {
    std::mutex mu;
    size_t max_items;
    size_t max_bytes;
    long long hits = 0;
    long long misses = 0;
    std::list<std::pair<std::string, Entry>> order;  // front = most recent
    std::unordered_map<std::string, std::list<std::pair<std::string, Entry>>::iterator> map;

    void init(size_t items, size_t bytes) {
        std::lock_guard<std::mutex> lk(mu);
        max_items = items;
        max_bytes = bytes;
        order.clear();
        map.clear();
        hits = misses = 0;
    }

    void trim_locked() {
        while (!order.empty() &&
               (order.size() > max_items || bytes_used() > max_bytes)) {
            auto last = order.end();
            --last;
            map.erase(last->first);
            order.pop_back();
        }
    }

    size_t bytes_used() const {
        size_t b = 0;
        for (auto& kv : order) b += kv.second.data.size();
        return b;
    }

    // Returns stored size on hit (0 on miss), touching recency and TTL.
    size_t peek(const char* key) {
        std::lock_guard<std::mutex> lk(mu);
        std::string k(key ? key : "");
        auto it = map.find(k);
        if (it == map.end()) { ++misses; return 0; }
        Entry& e = it->second->second;
        if (e.ttl_ms > 0 && now_ms() - e.born_ms > e.ttl_ms) {
            map.erase(k);
            order.erase(it->second);
            ++misses;
            return 0;
        }
        // move-to-front
        order.splice(order.begin(), order, it->second);
        ++hits;
        return e.data.size();
    }

    // Copies value into `out` (cap bytes). Returns 1 ok, 0 miss/expired, -1 too small.
    int read(const char* key, unsigned char* out, size_t cap) {
        std::lock_guard<std::mutex> lk(mu);
        std::string k(key ? key : "");
        auto it = map.find(k);
        if (it == map.end()) return 0;
        Entry& e = it->second->second;
        if (e.ttl_ms > 0 && now_ms() - e.born_ms > e.ttl_ms) {
            map.erase(k);
            order.erase(it->second);
            return 0;
        }
        if (e.data.size() > cap) return -1;
        if (cap && !e.data.empty()) memcpy(out, e.data.data(), e.data.size());
        return 1;
    }

    // 1 = stored, 0 = too large for cache.
    int put(const char* key, const unsigned char* data, size_t n, long long ttl_ms) {
        std::lock_guard<std::mutex> lk(mu);
        if (data && n > max_bytes) return 0;
        std::string k(key ? key : "");
        auto it = map.find(k);
        if (it != map.end()) {
            order.erase(it->second);
            map.erase(k);
        }
        Entry e;
        if (data) e.data.assign(reinterpret_cast<const char*>(data), n);
        e.born_ms = now_ms();
        e.ttl_ms = ttl_ms;
        order.emplace_front(k, std::move(e));
        map[k] = order.begin();
        trim_locked();
        return 1;
    }

    void del(const char* key) {
        std::lock_guard<std::mutex> lk(mu);
        std::string k(key ? key : "");
        auto it = map.find(k);
        if (it != map.end()) {
            order.erase(it->second);
            map.erase(k);
        }
    }

    void stats(long long* items, long long* bytes, long long* hits_out, long long* misses_out) {
        std::lock_guard<std::mutex> lk(mu);
        if (items) *items = static_cast<long long>(order.size());
        if (bytes) *bytes = static_cast<long long>(bytes_used());
        if (hits_out) *hits_out = hits;
        if (misses_out) *misses_out = misses;
    }
};

Lru g_cache;   // TTS audio + short-lived blobs
Lru g_sess;    // session message history (immortal per web process)

}  // namespace

// ---------------- C ABI ----------------

extern "C" {

// ---- generic TTL cache ----
void nn_cache_init(size_t items, size_t bytes) { g_cache.init(items, bytes); }
size_t nn_cache_peek(const char* key) { return g_cache.peek(key); }
int nn_cache_read(const char* key, unsigned char* out, size_t cap) { return g_cache.read(key, out, cap); }
int nn_cache_put(const char* key, const unsigned char* data, size_t n, long long ttl_ms) { return g_cache.put(key, data, n, ttl_ms); }
void nn_cache_stats(long long* items, long long* bytes, long long* hits, long long* misses) {
    g_cache.stats(items, bytes, hits, misses);
}

// ---- session store ----
void nn_sess_init(size_t items, size_t bytes) { g_sess.init(items, bytes); }
size_t nn_sess_peek(const char* key) { return g_sess.peek(key); }
int nn_sess_read(const char* key, unsigned char* out, size_t cap) { return g_sess.read(key, out, cap); }
int nn_sess_put(const char* key, const unsigned char* data, size_t n) { return g_sess.put(key, data, n, 0LL); }
void nn_sess_del(const char* key) { g_sess.del(key); }
void nn_sess_stats(long long* items, long long* bytes) { g_sess.stats(items, bytes, nullptr, nullptr); }

// ---- SSE frame:  "event: <kind>\ndata: <payload>\n\n"  (malloc'd, free with nn_free) ----
char* nn_sse_frame(const char* kind, const char* payload, size_t* outlen) {
    std::string s = "event: ";
    s += (kind ? kind : "");
    s += "\ndata: ";
    s += (payload ? payload : "");
    s += "\n\n";
    char* out = static_cast<char*>(malloc(s.size() + 1));
    if (out) {
        if (!s.empty()) memcpy(out, s.data(), s.size());
        out[s.size()] = '\0';
    }
    if (outlen) *outlen = s.size();
    return out;
}

// ---- truncate strings (tool args previews). Prefer ending on }, ] or ,  ----
char* nn_truncate_str(const char* raw, size_t maxlen) {
    std::string s(raw ? raw : "");
    if (s.size() > maxlen) {
        size_t cut = maxlen;
        size_t i = maxlen < s.size() ? maxlen : s.size();
        while (i > 0) {
            char c = s[i - 1];
            if (c == '}' || c == ']' || c == ',') { cut = i; break; }
            --i;
        }
        s = s.substr(0, cut);
    }
    char* out = static_cast<char*>(malloc(s.size() + 1));
    if (out) {
        if (!s.empty()) memcpy(out, s.data(), s.size());
        out[s.size()] = '\0';
    }
    return out;
}

void nn_free(void* p) { free(p); }

}  // extern "C"