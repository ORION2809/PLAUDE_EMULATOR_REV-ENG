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

SB_EXPORT char* sb_nemo_asr(const char* wav_path, const char* options, char** err) {
    Opts o(options);
    std::vector<float> pcm;
    int sr = 0;
    std::string e;
    if (!read_wav(wav_path, pcm, sr, e)) { if (err) *err = dup_c(e); return nullptr; }
    const std::string model = o.str("model"), diar = o.str("diar_model"), lang = o.str("language", "en-US");
    const int gpu = int(o.num("gpu", -1)), rc = int(o.num("right_context", 1));  // 1 = the CLI default (asr.streaming.rnnt_right_context)
    const long long peak0 = peak_rss_bytes();

    nemo_speech_asr_backend_config backend = {};
    backend.size = sizeof backend;
    backend.gpu = gpu;
    nemo_speech_asr_model_config mc = {};
    mc.size = sizeof mc;
    mc.path = model.c_str();
    nemo_speech_asr_streaming_config sc = {};
    sc.size = sizeof sc;
    sc.rnnt_right_context = rc;
    sc.chunk_size = 0.16f;  // runtime defaults (docs/asr/configuration.md), CTC-only knobs
    sc.ctc_left_padding = 1.92f;
    sc.ctc_right_padding = 1.92f;
    nemo_speech_asr_diar_config dc = {};
    dc.size = sizeof dc;
    dc.model_path = diar.empty() ? nullptr : diar.c_str();
    dc.left_context_frames = -1;
    nemo_speech_asr_recognizer_config cfg = {};
    cfg.size = sizeof cfg;
    cfg.backend = &backend;
    cfg.model = &mc;
    cfg.streaming = &sc;
    if (!diar.empty()) cfg.diar = &dc;

    const double t0 = now_s();
    nemo_speech_asr_recognizer* rec = nullptr;
    if (nemo_speech_asr_create(&cfg, &rec) != NEMO_SPEECH_ASR_OK) return fail(err, "nemo_speech_asr_create");
    const double t1 = now_s();
    nemo_speech_asr_recognition_options ro = nemo_speech_asr_recognition_options_default();
    ro.enable_word_time_offsets = true;
    ro.enable_automatic_punctuation = o.num("punctuation", 1) != 0;
    ro.language_code = lang.empty() ? nullptr : lang.c_str();
    ro.enable_speaker_diarization = !diar.empty();
    nemo_speech_asr_result* res = nullptr;
    if (nemo_speech_asr_recognize_f32(rec, &ro, pcm.data(), pcm.size(), sr, &res) != NEMO_SPEECH_ASR_OK || !res) {
        nemo_speech_asr_destroy(rec);
        return fail(err, "nemo_speech_asr_recognize_f32");
    }
    const double t2 = now_s();
    std::vector<Word> words;
    const size_t n = nemo_speech_asr_result_word_count(res, 0);
    for (size_t i = 0; i < n; i++) {
        const char* w = nemo_speech_asr_result_word_text(res, 0, i);
        words.push_back({w ? w : "", nemo_speech_asr_result_word_start_time(res, 0, i) / 1000.0,
                         nemo_speech_asr_result_word_end_time(res, 0, i) / 1000.0,
                         diar.empty() ? 0 : nemo_speech_asr_result_word_speaker_tag(res, 0, i)});
    }
    nemo_speech_asr_result_destroy(res);
    nemo_speech_asr_destroy(rec);
    const long long peak1 = peak_rss_bytes();
    std::string settings = "{\"language\":" + jstr(lang) + ",\"right_context\":" + std::to_string(rc) +
                           ",\"gpu\":" + std::to_string(gpu) + ",\"runtime\":" + jstr(nemo_speech_asr_version()) +
                           ",\"punctuation\":" + (ro.enable_automatic_punctuation ? "true" : "false") + "}";
    std::string models = "{\"asr\":{\"path\":" + jstr(model) + ",\"bytes\":" + std::to_string(file_bytes(model)) + "}" +
                         (diar.empty() ? "" : ",\"diarization\":{\"path\":" + jstr(diar) + ",\"bytes\":" +
                                                  std::to_string(file_bytes(diar)) + "}") + "}";
    return dup_c(result_json(diar.empty() ? "nemo-asr" : "nemo-asr+tags", settings, models, pcm.size() / double(sr),
                             t1 - t0, t2 - t1, peak0, peak1, words, {}));
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
