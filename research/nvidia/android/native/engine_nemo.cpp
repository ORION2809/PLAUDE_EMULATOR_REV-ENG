// speechbench engine: NVIDIA NeMo-Speech.cpp through its public C ABI
// (include/nemo_speech/{asr,diar}.h, NeMo-Speech.cpp 0f706e4).
//
//   sb_nemo_asr   Nemotron ASR (nemo_speech_asr_recognize_f32), words with times;
//                 with diar_model= the runtime also tags each word with a
//                 Sortformer speaker (what `nemo-speech transcribe --diarize` does)
//   sb_nemo_diar  Sortformer diarization (stream: 160 ms pushes, as the CLI and
//                 NVIDIA's diarize_file example do), segments under the library's
//                 default postprocessing
//
// Options (key=value;...): model, diar_model, language (default en-US),
// right_context (default 1 = the CLI's asr.streaming.rnnt_right_context; -1 = model
// default), gpu (-1 = CPU), preset, punctuation (1).
#include "nemo_speech/asr.h"
#include "nemo_speech/diar.h"
#include "sb_common.h"

using namespace sb;

static char* fail(char** err, const std::string& what) {
    const char* e = nemo_speech_asr_last_error();
    if (err) *err = dup_c(what + (e && *e ? std::string(": ") + e : std::string()));
    return nullptr;
}

// One recognizer + stream. The strings the C ABI configs point at live here, so the
// session owns everything the runtime may read later.
struct NemoSession {
    std::string model, diar, lang;
    int gpu = -1, rc = 1;
    bool punctuation = true;
    nemo_speech_asr_backend_config backend = {};
    nemo_speech_asr_model_config mc = {};
    nemo_speech_asr_streaming_config sc = {};
    nemo_speech_asr_diar_config dc = {};
    nemo_speech_asr_recognizer_config cfg = {};
    nemo_speech_asr_recognition_options ro = {};
    nemo_speech_asr_recognizer* rec = nullptr;
    nemo_speech_asr_stream* stream = nullptr;
    std::vector<Word> words;   // words of final results, in order
    std::string partial;       // text of the latest interim result
    double audio_s = 0, load_s = 0, process_s = 0;
    long long peak0 = 0;
};

static bool nemo_create(NemoSession& s, const Opts& o, bool streaming, std::string& err) {
    s.model = o.str("model"); s.diar = o.str("diar_model"); s.lang = o.str("language", "en-US");
    s.gpu = int(o.num("gpu", -1));
    s.rc = int(o.num("right_context", 1));  // 1 = the CLI default (asr.streaming.rnnt_right_context)
    s.punctuation = o.num("punctuation", 1) != 0;
    s.peak0 = peak_rss_bytes();
    s.backend.size = sizeof s.backend; s.backend.gpu = s.gpu;
    s.mc.size = sizeof s.mc; s.mc.path = s.model.c_str();
    s.sc.size = sizeof s.sc; s.sc.rnnt_right_context = s.rc;
    s.sc.chunk_size = 0.16f;  // runtime defaults (docs/asr/configuration.md), CTC-only knobs
    s.sc.ctc_left_padding = 1.92f; s.sc.ctc_right_padding = 1.92f;
    s.dc.size = sizeof s.dc; s.dc.model_path = s.diar.empty() ? nullptr : s.diar.c_str(); s.dc.left_context_frames = -1;
    s.cfg.size = sizeof s.cfg; s.cfg.backend = &s.backend; s.cfg.model = &s.mc; s.cfg.streaming = &s.sc;
    if (!s.diar.empty()) s.cfg.diar = &s.dc;
    const double t0 = now_s();
    if (nemo_speech_asr_create(&s.cfg, &s.rec) != NEMO_SPEECH_ASR_OK) {
        const char* e = nemo_speech_asr_last_error(); err = std::string("nemo_speech_asr_create: ") + (e ? e : ""); return false;
    }
    s.ro = nemo_speech_asr_recognition_options_default();
    s.ro.enable_word_time_offsets = true;
    s.ro.enable_automatic_punctuation = s.punctuation;
    s.ro.language_code = s.lang.empty() ? nullptr : s.lang.c_str();
    s.ro.enable_speaker_diarization = !s.diar.empty();
    s.ro.interim_results = streaming;
    if (streaming && nemo_speech_asr_streaming_recognize(s.rec, &s.ro, &s.stream) != NEMO_SPEECH_ASR_OK) {
        const char* e = nemo_speech_asr_last_error(); err = std::string("nemo_speech_asr_streaming_recognize: ") + (e ? e : "");
        nemo_speech_asr_destroy(s.rec); s.rec = nullptr; return false;
    }
    s.load_s = now_s() - t0;
    return true;
}

static void collect_words(NemoSession& s, nemo_speech_asr_result* r) {
    const size_t n = nemo_speech_asr_result_word_count(r, 0);
    for (size_t i = 0; i < n; i++) {
        const char* w = nemo_speech_asr_result_word_text(r, 0, i);
        s.words.push_back({w ? w : "", nemo_speech_asr_result_word_start_time(r, 0, i) / 1000.0,
                           nemo_speech_asr_result_word_end_time(r, 0, i) / 1000.0,
                           s.diar.empty() ? 0 : nemo_speech_asr_result_word_speaker_tag(r, 0, i)});
    }
}

// Pull every result the stream has ready; finals add words, interims update the live text.
static bool drain(NemoSession& s, std::string& err) {
    for (;;) {
        nemo_speech_asr_result* r = nullptr;
        if (nemo_speech_asr_stream_next(s.stream, &r) != NEMO_SPEECH_ASR_OK) {
            const char* e = nemo_speech_asr_last_error(); err = std::string("nemo_speech_asr_stream_next: ") + (e ? e : ""); return false;
        }
        if (!r) return true;
        if (nemo_speech_asr_result_is_final(r)) { collect_words(s, r); s.partial.clear(); }
        else { const char* t = nemo_speech_asr_result_transcript(r, 0); s.partial = t ? t : ""; }
        nemo_speech_asr_result_destroy(r);
    }
}

static bool session_push(NemoSession& s, const float* x, size_t n, int sr, std::string& err) {
    const double t0 = now_s();
    if (nemo_speech_asr_stream_push_f32(s.stream, x, n, sr) != NEMO_SPEECH_ASR_OK) {
        const char* e = nemo_speech_asr_last_error(); err = std::string("nemo_speech_asr_stream_push_f32: ") + (e ? e : ""); return false;
    }
    const bool ok = drain(s, err);
    s.process_s += now_s() - t0;
    s.audio_s += double(n) / (sr > 0 ? sr : 16000);
    return ok;
}

static bool session_finish(NemoSession& s, std::string& err) {
    const double t0 = now_s();
    if (nemo_speech_asr_stream_finish(s.stream) != NEMO_SPEECH_ASR_OK) {
        const char* e = nemo_speech_asr_last_error(); err = std::string("nemo_speech_asr_stream_finish: ") + (e ? e : ""); return false;
    }
    const bool ok = drain(s, err);
    s.process_s += now_s() - t0;
    return ok;
}

static void session_close(NemoSession& s) {
    if (s.stream) nemo_speech_asr_stream_close(s.stream);
    if (s.rec) nemo_speech_asr_destroy(s.rec);
    s.stream = nullptr; s.rec = nullptr;
}

static std::string session_json(const NemoSession& s, bool streaming) {
    std::string settings = "{\"language\":" + jstr(s.lang) + ",\"right_context\":" + std::to_string(s.rc) +
                           ",\"gpu\":" + std::to_string(s.gpu) + ",\"runtime\":" + jstr(nemo_speech_asr_version()) +
                           ",\"punctuation\":" + (s.punctuation ? "true" : "false") +
                           ",\"api\":" + (streaming ? "\"streaming (push 1.12 s chunks)\"" : "\"recognize_f32 (one shot)\"") + "}";
    std::string models = "{\"asr\":{\"path\":" + jstr(s.model) + ",\"bytes\":" + std::to_string(file_bytes(s.model)) + "}" +
                         (s.diar.empty() ? "" : ",\"diarization\":{\"path\":" + jstr(s.diar) + ",\"bytes\":" +
                                                    std::to_string(file_bytes(s.diar)) + "}") + "}";
    return result_json(s.diar.empty() ? "nemo-asr" : "nemo-asr+tags", settings, models, s.audio_s, s.load_s, s.process_s,
                       s.peak0, peak_rss_bytes(), s.words, {});
}

// Options: model, diar_model, language (en-US), right_context (1), gpu (-1), punctuation (1),
// stream (0 = one recognize_f32 call over the whole file; 1 = the streaming API, 1.12 s pushes).
SB_EXPORT char* sb_nemo_asr(const char* wav_path, const char* options, char** err) {
    Opts o(options);
    std::vector<float> pcm;
    int sr = 0;
    std::string e;
    if (!read_wav(wav_path, pcm, sr, e)) { if (err) *err = dup_c(e); return nullptr; }
    const bool streaming = o.num("stream", 0) != 0;
    NemoSession s;
    if (!nemo_create(s, o, streaming, e)) { if (err) *err = dup_c(e); return nullptr; }
    if (streaming) {
        const size_t step = size_t(sr) * 112 / 100;  // 1.12 s, one encoder chunk at right_context 13
        for (size_t off = 0; off < pcm.size(); off += step)
            if (!session_push(s, pcm.data() + off, std::min(step, pcm.size() - off), sr, e)) { session_close(s); if (err) *err = dup_c(e); return nullptr; }
        if (!session_finish(s, e)) { session_close(s); if (err) *err = dup_c(e); return nullptr; }
    } else {
        const double t0 = now_s();
        nemo_speech_asr_result* res = nullptr;
        if (nemo_speech_asr_recognize_f32(s.rec, &s.ro, pcm.data(), pcm.size(), sr, &res) != NEMO_SPEECH_ASR_OK || !res) {
            session_close(s);
            return fail(err, "nemo_speech_asr_recognize_f32");
        }
        s.process_s = now_s() - t0;
        s.audio_s = pcm.size() / double(sr);
        collect_words(s, res);
        nemo_speech_asr_result_destroy(res);
    }
    session_close(s);
    return dup_c(session_json(s, streaming));
}

// ---- live session API (the app records into it) -------------------------------------
SB_EXPORT void* sb_nemo_session_create(const char* options, char** err) {
    Opts o(options);
    auto* s = new NemoSession();
    std::string e;
    if (!nemo_create(*s, o, true, e)) { delete s; if (err) *err = dup_c(e); return nullptr; }
    return s;
}

// Push mono float samples; returns the live text so far (finals + the current interim), malloc'd.
SB_EXPORT char* sb_nemo_session_push(void* h, const float* samples, size_t n, int sample_rate, char** err) {
    auto* s = static_cast<NemoSession*>(h);
    std::string e;
    if (!session_push(*s, samples, n, sample_rate, e)) { if (err) *err = dup_c(e); return nullptr; }
    std::string text;
    for (const Word& w : s->words) text += (text.empty() ? "" : " ") + w.w;
    if (!s->partial.empty()) text += (text.empty() ? "" : " ") + s->partial;
    return dup_c(text);
}

// No more audio: flush, return the result JSON (words with times), and free the session.
SB_EXPORT char* sb_nemo_session_finish(void* h, char** err) {
    auto* s = static_cast<NemoSession*>(h);
    std::string e;
    const bool ok = session_finish(*s, e);
    session_close(*s);
    std::string out = ok ? session_json(*s, true) : "";
    delete s;
    if (!ok) { if (err) *err = dup_c(e); return nullptr; }
    return dup_c(out);
}

SB_EXPORT void sb_nemo_session_cancel(void* h) {
    auto* s = static_cast<NemoSession*>(h);
    session_close(*s);
    delete s;
}

SB_EXPORT char* sb_nemo_diar(const char* wav_path, const char* options, char** err) {
    Opts o(options);
    std::vector<float> pcm;
    int sr = 0;
    std::string e;
    if (!read_wav(wav_path, pcm, sr, e)) { if (err) *err = dup_c(e); return nullptr; }
    const std::string model = o.str("diar_model", o.str("model")), preset = o.str("preset");
    const int gpu = int(o.num("gpu", -1));
    const long long peak0 = peak_rss_bytes();
    nemo_speech_diar_model_config cfg = {};
    cfg.size = sizeof cfg;
    cfg.model_path = model.c_str();
    cfg.gpu = gpu;
    cfg.preset = preset.empty() ? nullptr : preset.c_str();
    cfg.left_context_frames = -1;
    const double t0 = now_s();
    nemo_speech_diar_model* m = nullptr;
    if (nemo_speech_diar_create(&cfg, &m) != NEMO_SPEECH_ASR_OK) return fail(err, "nemo_speech_diar_create");
    const double t1 = now_s();
    nemo_speech_diar_stream* job = nullptr;
    if (nemo_speech_diar_stream_open(m, &job) != NEMO_SPEECH_ASR_OK) {
        nemo_speech_diar_destroy(m);
        return fail(err, "nemo_speech_diar_stream_open");
    }
    const size_t push = size_t(sr) * 160 / 1000;
    for (size_t off = 0; off < pcm.size(); off += push) {
        if (nemo_speech_diar_stream_push_f32(job, pcm.data() + off, std::min(push, pcm.size() - off), sr) != NEMO_SPEECH_ASR_OK) {
            nemo_speech_diar_stream_close(job);
            nemo_speech_diar_destroy(m);
            return fail(err, "nemo_speech_diar_stream_push_f32");
        }
    }
    if (nemo_speech_diar_stream_finish(job) != NEMO_SPEECH_ASR_OK) {
        nemo_speech_diar_stream_close(job);
        nemo_speech_diar_destroy(m);
        return fail(err, "nemo_speech_diar_stream_finish");
    }
    size_t count = 0;
    nemo_speech_diar_segments(job, nullptr, nullptr, 0, &count);
    std::vector<nemo_speech_diar_segment> segs(count);
    if (count && nemo_speech_diar_segments(job, nullptr, segs.data(), segs.size(), &count) != NEMO_SPEECH_ASR_OK) {
        nemo_speech_diar_stream_close(job);
        nemo_speech_diar_destroy(m);
        return fail(err, "nemo_speech_diar_segments");
    }
    const double t2 = now_s();
    std::vector<Turn> turns;
    for (size_t i = 0; i < count; i++) turns.push_back({segs[i].start_time, segs[i].end_time, segs[i].speaker});
    const int max_spk = nemo_speech_diar_num_speakers(m);
    nemo_speech_diar_stream_close(job);
    nemo_speech_diar_destroy(m);
    const long long peak1 = peak_rss_bytes();
    std::string settings = "{\"preset\":" + jstr(preset) + ",\"gpu\":" + std::to_string(gpu) +
                           ",\"push_ms\":160,\"postprocessing\":\"library defaults\",\"max_speakers\":" + std::to_string(max_spk) +
                           ",\"runtime\":" + jstr(nemo_speech_asr_version()) + "}";
    std::string models = "{\"diarization\":{\"path\":" + jstr(model) + ",\"bytes\":" + std::to_string(file_bytes(model)) + "}}";
    return dup_c(result_json("nemo-diar", settings, models, pcm.size() / double(sr), t1 - t0, t2 - t1, peak0, peak1, {}, turns));
}

SB_EXPORT void sb_nemo_free(char* p) { std::free(p); }
