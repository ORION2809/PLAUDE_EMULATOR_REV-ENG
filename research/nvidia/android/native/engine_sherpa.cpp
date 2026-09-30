// speechbench engine: sherpa-onnx speaker diarization (k2-fsa/sherpa-onnx v1.13.8
// C API), the on-device form of the current stack's diarizer: pyannote
// segmentation-3.0 + 3D-Speaker CAM++ embeddings + fast clustering, with the
// harness's dev-calibrated cluster_threshold 1.15 (docs/v5-test-split.md) and
// its min_duration_on/off 0.3/0.5, 4 threads (pipeline/whisper_sherpa.py).
//
// Options: segmentation, embedding, threshold (1.15), num_speakers (0 = unknown),
// min_on (0.3), min_off (0.5), threads (4).
#include "sb_common.h"
#include "sherpa-onnx/c-api/c-api.h"

using namespace sb;

SB_EXPORT char* sb_sherpa_diar(const char* wav_path, const char* options, char** err) {
    Opts o(options);
    std::vector<float> pcm;
    int sr = 0;
    std::string e;
    if (!read_wav(wav_path, pcm, sr, e)) { if (err) *err = dup_c(e); return nullptr; }
    const std::string seg = o.str("segmentation"), emb = o.str("embedding");
    const int threads = int(o.num("threads", 4)), nspk = int(o.num("num_speakers", 0));
    const float thr = float(o.real("threshold", 1.15)), mon = float(o.real("min_on", 0.3)), moff = float(o.real("min_off", 0.5));
    const long long peak0 = peak_rss_bytes();
    SherpaOnnxOfflineSpeakerDiarizationConfig cfg;
    std::memset(&cfg, 0, sizeof cfg);
    cfg.segmentation.pyannote.model = seg.c_str();
    cfg.segmentation.num_threads = threads;
    cfg.segmentation.provider = "cpu";
    cfg.embedding.model = emb.c_str();
    cfg.embedding.num_threads = threads;
    cfg.embedding.provider = "cpu";
    cfg.clustering.num_clusters = nspk;
    cfg.clustering.threshold = thr;
    cfg.min_duration_on = mon;
    cfg.min_duration_off = moff;
    const double t0 = now_s();
    const SherpaOnnxOfflineSpeakerDiarization* sd = SherpaOnnxCreateOfflineSpeakerDiarization(&cfg);
    if (!sd) { if (err) *err = dup_c("SherpaOnnxCreateOfflineSpeakerDiarization failed"); return nullptr; }
    if (SherpaOnnxOfflineSpeakerDiarizationGetSampleRate(sd) != sr) {
        SherpaOnnxDestroyOfflineSpeakerDiarization(sd);
        if (err) *err = dup_c("sample rate mismatch");
        return nullptr;
    }
    const double t1 = now_s();
    const SherpaOnnxOfflineSpeakerDiarizationResult* r = SherpaOnnxOfflineSpeakerDiarizationProcess(sd, pcm.data(), int32_t(pcm.size()));
    if (!r) {
        SherpaOnnxDestroyOfflineSpeakerDiarization(sd);
        if (err) *err = dup_c("SherpaOnnxOfflineSpeakerDiarizationProcess failed");
        return nullptr;
    }
    const double t2 = now_s();
    std::vector<Turn> turns;
    const int n = SherpaOnnxOfflineSpeakerDiarizationResultGetNumSegments(r);
    const SherpaOnnxOfflineSpeakerDiarizationSegment* s = SherpaOnnxOfflineSpeakerDiarizationResultSortByStartTime(r);
    for (int i = 0; i < n; i++) turns.push_back({s[i].start, s[i].end, s[i].speaker + 1});
    SherpaOnnxOfflineSpeakerDiarizationDestroySegment(s);
    SherpaOnnxOfflineSpeakerDiarizationDestroyResult(r);
    SherpaOnnxDestroyOfflineSpeakerDiarization(sd);
    const long long peak1 = peak_rss_bytes();
    std::string settings = "{\"threshold\":" + jnum(thr) + ",\"num_speakers\":" + std::to_string(nspk) + ",\"min_on\":" +
                           jnum(mon) + ",\"min_off\":" + jnum(moff) + ",\"threads\":" + std::to_string(threads) +
                           ",\"runtime\":" + jstr(SherpaOnnxGetVersionStr()) + "}";
    std::string models = "{\"segmentation\":{\"path\":" + jstr(seg) + ",\"bytes\":" + std::to_string(file_bytes(seg)) +
                         "},\"embedding\":{\"path\":" + jstr(emb) + ",\"bytes\":" + std::to_string(file_bytes(emb)) + "}}";
    return dup_c(result_json("sherpa-onnx-diar", settings, models, pcm.size() / double(sr), t1 - t0, t2 - t1, peak0, peak1, {}, turns));
}

SB_EXPORT void sb_sherpa_free(char* p) { std::free(p); }
