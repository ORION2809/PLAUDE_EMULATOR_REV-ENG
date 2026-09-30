// speechbench engine: whisper.cpp (ggml-org/whisper.cpp v1.9.4, include/whisper.h),
// the on-device form of the current stack's ASR (OpenAI small.en weights).
//
// Decoding mirrors `whisper-cli -ojf` (research/nvidia runs on the Mac): beam
// search 5, best_of 5, temperature fallback +0.2, entropy 2.4, logprob -1.0,
// token timestamps on, flash attention on, 4 threads, language en.  Words are
// runs of tokens from one space-prefixed token to the next, timed by the
// tokens' t0/t1 (pipeline/whisper_cpp.py does the same with the CLI's JSON).
//
// Options: model, threads (4), beam_size (5), language (en), gpu (0 = CPU, 1 = GPU).
#include "sb_common.h"
#include "whisper.h"

using namespace sb;

SB_EXPORT char* sb_whisper_asr(const char* wav_path, const char* options, char** err) {
    Opts o(options);
    std::vector<float> pcm;
    int sr = 0;
    std::string e;
    if (!read_wav(wav_path, pcm, sr, e)) { if (err) *err = dup_c(e); return nullptr; }
    if (sr != WHISPER_SAMPLE_RATE) { if (err) *err = dup_c("whisper.cpp needs 16 kHz audio"); return nullptr; }
    const std::string model = o.str("model"), lang = o.str("language", "en");
    const int threads = int(o.num("threads", 4)), beam = int(o.num("beam_size", 5));
    const bool gpu = o.num("gpu", 0) != 0;
    const long long peak0 = peak_rss_bytes();
    whisper_context_params cp = whisper_context_default_params();
    cp.use_gpu = gpu;
    cp.flash_attn = true;
    const double t0 = now_s();
    whisper_context* ctx = whisper_init_from_file_with_params(model.c_str(), cp);
    if (!ctx) { if (err) *err = dup_c("whisper_init_from_file_with_params failed: " + model); return nullptr; }
    const double t1 = now_s();
    whisper_full_params wp = whisper_full_default_params(beam > 1 ? WHISPER_SAMPLING_BEAM_SEARCH : WHISPER_SAMPLING_GREEDY);
    wp.print_realtime = wp.print_progress = wp.print_timestamps = wp.print_special = false;
    wp.language = lang.c_str();
    wp.n_threads = threads;
    wp.token_timestamps = true;
    wp.beam_search.beam_size = beam;
    wp.greedy.best_of = 5;
    wp.temperature = 0.0f;
    wp.temperature_inc = 0.2f;
    wp.entropy_thold = 2.40f;
    wp.logprob_thold = -1.00f;
    if (whisper_full(ctx, wp, pcm.data(), int(pcm.size())) != 0) {
        whisper_free(ctx);
        if (err) *err = dup_c("whisper_full failed");
        return nullptr;
    }
    const double t2 = now_s();
    std::vector<Word> words;
    const whisper_token eot = whisper_token_eot(ctx);
    for (int s = 0; s < whisper_full_n_segments(ctx); s++) {
        for (int t = 0; t < whisper_full_n_tokens(ctx, s); t++) {
            whisper_token_data d = whisper_full_get_token_data(ctx, s, t);
            if (d.id >= eot) continue;  // special tokens ([_BEG_], timestamps, ...)
            const char* txt = whisper_full_get_token_text(ctx, s, t);
            std::string text = txt ? txt : "";
            if (text.empty()) continue;
            const double a = d.t0 / 100.0, b = d.t1 / 100.0;
            if (text[0] == ' ' || words.empty()) words.push_back({text, a, b, 0});
            else { words.back().w += text; words.back().end = std::max(words.back().end, b); }
        }
    }
    for (Word& w : words) if (!w.w.empty() && w.w[0] == ' ') w.w.erase(0, 1);
    whisper_free(ctx);
    const long long peak1 = peak_rss_bytes();
    std::string settings = "{\"language\":" + jstr(lang) + ",\"threads\":" + std::to_string(threads) + ",\"beam_size\":" +
                           std::to_string(beam) + ",\"gpu\":" + (gpu ? "true" : "false") +
                           ",\"flash_attn\":true,\"word_times\":\"token t0/t1\",\"runtime\":" + jstr(whisper_print_system_info()) + "}";
    std::string models = "{\"asr\":{\"path\":" + jstr(model) + ",\"bytes\":" + std::to_string(file_bytes(model)) + "}}";
    return dup_c(result_json("whisper.cpp", settings, models, pcm.size() / double(sr), t1 - t0, t2 - t1, peak0, peak1, words, {}));
}

SB_EXPORT void sb_whisper_free(char* p) { std::free(p); }
