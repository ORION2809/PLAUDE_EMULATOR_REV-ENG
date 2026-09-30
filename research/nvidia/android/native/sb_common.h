// speechbench: shared helpers for the on-device engines (docs/nvidia-speech.md).
// WAV reading, a key=value option bag, a minimal JSON writer and process
// memory/timing probes. Header-only so each engine library carries its own
// (hidden) copy.
#pragma once

#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <string>
#include <vector>

#if defined(__APPLE__)
#include <sys/resource.h>
#endif

#define SB_EXPORT extern "C" __attribute__((visibility("default")))

namespace sb {

// ---- options: "key=value;key=value" -------------------------------------------------
struct Opts {
    std::map<std::string, std::string> kv;
    explicit Opts(const char* s) {
        std::string all = s ? s : "", item;
        size_t i = 0;
        while (i <= all.size()) {
            size_t j = all.find(';', i);
            if (j == std::string::npos) j = all.size();
            item = all.substr(i, j - i);
            size_t eq = item.find('=');
            if (eq != std::string::npos) kv[item.substr(0, eq)] = item.substr(eq + 1);
            i = j + 1;
        }
    }
    std::string str(const std::string& k, const std::string& d = "") const {
        auto it = kv.find(k);
        return it == kv.end() ? d : it->second;
    }
    long num(const std::string& k, long d) const {
        auto it = kv.find(k);
        return it == kv.end() || it->second.empty() ? d : std::strtol(it->second.c_str(), nullptr, 10);
    }
    double real(const std::string& k, double d) const {
        auto it = kv.find(k);
        return it == kv.end() || it->second.empty() ? d : std::strtod(it->second.c_str(), nullptr);
    }
};

// ---- WAV: RIFF PCM16 or float32, mono, 16 kHz --------------------------------------
inline bool read_wav(const std::string& path, std::vector<float>& out, int& sample_rate, std::string& err) {
    FILE* f = std::fopen(path.c_str(), "rb");
    if (!f) { err = "cannot open " + path; return false; }
    std::vector<uint8_t> buf;
    std::fseek(f, 0, SEEK_END);
    long n = std::ftell(f);
    std::fseek(f, 0, SEEK_SET);
    buf.resize(n > 0 ? size_t(n) : 0);
    if (n > 0 && std::fread(buf.data(), 1, buf.size(), f) != buf.size()) { std::fclose(f); err = "short read"; return false; }
    std::fclose(f);
    if (buf.size() < 12 || std::memcmp(buf.data(), "RIFF", 4) || std::memcmp(buf.data() + 8, "WAVE", 4)) {
        err = "not a RIFF/WAVE file"; return false;
    }
    uint16_t fmt = 0, channels = 0, bits = 0;
    uint32_t rate = 0;
    size_t pos = 12;
    const uint8_t* data = nullptr;
    size_t data_len = 0;
    auto u16 = [&](size_t p) { return uint16_t(buf[p] | (buf[p + 1] << 8)); };
    auto u32 = [&](size_t p) { return uint32_t(buf[p] | (buf[p + 1] << 8) | (buf[p + 2] << 16) | (uint32_t(buf[p + 3]) << 24)); };
    while (pos + 8 <= buf.size()) {
        uint32_t len = u32(pos + 4);
        if (!std::memcmp(buf.data() + pos, "fmt ", 4) && pos + 24 <= buf.size()) {
            fmt = u16(pos + 8); channels = u16(pos + 10); rate = u32(pos + 12); bits = u16(pos + 22);
            if (fmt == 0xFFFE && pos + 34 <= buf.size()) fmt = u16(pos + 32);  // WAVE_FORMAT_EXTENSIBLE subformat
        } else if (!std::memcmp(buf.data() + pos, "data", 4)) {
            data = buf.data() + pos + 8;
            data_len = std::min<size_t>(len, buf.size() - pos - 8);
        }
        pos += 8 + len + (len & 1);
    }
    if (!data || channels != 1) { err = "need mono WAV with a data chunk"; return false; }
    sample_rate = int(rate);
    if (fmt == 1 && bits == 16) {
        out.resize(data_len / 2);
        for (size_t i = 0; i < out.size(); i++) out[i] = int16_t(data[2 * i] | (data[2 * i + 1] << 8)) / 32768.0f;
    } else if (fmt == 3 && bits == 32) {
        out.resize(data_len / 4);
        std::memcpy(out.data(), data, out.size() * 4);
    } else {
        err = "unsupported WAV encoding (need PCM16 or float32)"; return false;
    }
    return true;
}

// ---- JSON --------------------------------------------------------------------------
inline std::string jstr(const std::string& s) {
    std::string o = "\"";
    for (unsigned char c : s) {
        switch (c) {
            case '"': o += "\\\""; break;
            case '\\': o += "\\\\"; break;
            case '\n': o += "\\n"; break;
            case '\r': o += "\\r"; break;
            case '\t': o += "\\t"; break;
            default:
                if (c < 0x20) { char b[8]; std::snprintf(b, sizeof b, "\\u%04x", c); o += b; }
                else o += char(c);
        }
    }
    return o + "\"";
}
inline std::string jnum(double v) {
    if (!std::isfinite(v)) return "null";
    char b[40];
    std::snprintf(b, sizeof b, "%.6g", v);
    return b;
}

// ---- process probes ----------------------------------------------------------------
inline double now_s() {
    return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();
}
// Peak resident set size of this process in bytes (Linux/Android VmHWM, macOS ru_maxrss).
inline long long peak_rss_bytes() {
#if defined(__APPLE__)
    struct rusage ru {};
    getrusage(RUSAGE_SELF, &ru);
    return (long long)ru.ru_maxrss;
#else
    FILE* f = std::fopen("/proc/self/status", "r");
    if (!f) return -1;
    char line[256];
    long long kb = -1;
    while (std::fgets(line, sizeof line, f))
        if (!std::strncmp(line, "VmHWM:", 6)) kb = std::atoll(line + 6);
    std::fclose(f);
    return kb < 0 ? -1 : kb * 1024;
#endif
}
inline long long file_bytes(const std::string& p) {
    FILE* f = std::fopen(p.c_str(), "rb");
    if (!f) return -1;
    std::fseek(f, 0, SEEK_END);
    long long n = std::ftell(f);
    std::fclose(f);
    return n;
}

inline char* dup_c(const std::string& s) {
    char* p = static_cast<char*>(std::malloc(s.size() + 1));
    std::memcpy(p, s.c_str(), s.size() + 1);
    return p;
}

// One word or turn of a result.
struct Word { std::string w; double start, end; int speaker; };
struct Turn { double start, end; int speaker; };

// The result document every engine returns.
inline std::string result_json(const std::string& engine, const std::string& settings_json, const std::string& models_json,
                               double audio_s, double load_s, double process_s, long long peak_before, long long peak_after,
                               const std::vector<Word>& words, const std::vector<Turn>& turns) {
    std::string o = "{\"schema\":\"plaud-harness/speechbench/1\",\"engine\":" + jstr(engine) +
                    ",\"settings\":" + settings_json + ",\"models\":" + models_json +
                    ",\"timing\":{\"audio_s\":" + jnum(audio_s) + ",\"load_s\":" + jnum(load_s) +
                    ",\"process_s\":" + jnum(process_s) + ",\"rtf\":" + jnum(audio_s > 0 ? process_s / audio_s : NAN) +
                    "},\"memory\":{\"peak_rss_bytes_before_load\":" + std::to_string(peak_before) +
                    ",\"peak_rss_bytes\":" + std::to_string(peak_after) + "},\"words\":[";
    for (size_t i = 0; i < words.size(); i++) {
        const Word& w = words[i];
        o += (i ? "," : "") + std::string("{\"w\":") + jstr(w.w) + ",\"start\":" + jnum(w.start) + ",\"end\":" + jnum(w.end);
        if (w.speaker > 0) o += ",\"speaker\":" + std::to_string(w.speaker);
        o += "}";
    }
    o += "],\"turns\":[";
    for (size_t i = 0; i < turns.size(); i++)
        o += (i ? "," : "") + std::string("{\"start\":") + jnum(turns[i].start) + ",\"end\":" + jnum(turns[i].end) +
             ",\"speaker\":" + std::to_string(turns[i].speaker) + "}";
    return o + "]}";
}

}  // namespace sb
