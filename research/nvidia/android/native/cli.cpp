// speechbench CLI: run one engine on one WAV and print its JSON result.
//
//   speechbench <engine> <audio.wav> [key=value ...] [--out result.json]
//   engines: nemo-asr, nemo-diar, whisper, sherpa-diar
//
// One engine per process, so the reported peak RSS (VmHWM / ru_maxrss) belongs to
// that engine alone. Used by research/nvidia/android/bench.sh over adb and on the Mac.
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

#include "speechbench_api.h"

int main(int argc, char** argv) {
    if (argc < 3) {
        std::fprintf(stderr, "usage: %s nemo-asr|nemo-diar|whisper|sherpa-diar <audio.wav> [key=value ...] [--out file]\n", argv[0]);
        return 2;
    }
    const std::string engine = argv[1];
    std::string opts, out;
    for (int i = 3; i < argc; i++) {
        if (!std::strcmp(argv[i], "--out") && i + 1 < argc) { out = argv[++i]; continue; }
        opts += (opts.empty() ? "" : ";") + std::string(argv[i]);
    }
    char* err = nullptr;
    char* res = nullptr;
    if (engine == "nemo-asr") res = sb_nemo_asr(argv[2], opts.c_str(), &err);
    else if (engine == "nemo-diar") res = sb_nemo_diar(argv[2], opts.c_str(), &err);
#ifndef SB_NO_WHISPER
    else if (engine == "whisper") res = sb_whisper_asr(argv[2], opts.c_str(), &err);
#endif
#ifndef SB_NO_SHERPA
    else if (engine == "sherpa-diar") res = sb_sherpa_diar(argv[2], opts.c_str(), &err);
#endif
    else { std::fprintf(stderr, "unknown or disabled engine: %s\n", engine.c_str()); return 2; }
    if (!res) { std::fprintf(stderr, "speechbench %s failed: %s\n", engine.c_str(), err ? err : "?"); std::free(err); return 1; }
    if (out.empty()) std::puts(res);
    else {
        FILE* f = std::fopen(out.c_str(), "w");
        if (!f) { std::fprintf(stderr, "cannot write %s\n", out.c_str()); std::free(res); return 1; }
        std::fputs(res, f);
        std::fputc('\n', f);
        std::fclose(f);
    }
    std::free(res);
    return 0;
}
