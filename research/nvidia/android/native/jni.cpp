// JNI bridge for the SpeechBench Android app (research/nvidia/android/app):
// com.plaudharness.speechbench.NativeBench.run(engine, wavPath, options) -> JSON.
#include <jni.h>

#include <cstdlib>
#include <string>

#include "speechbench_api.h"

static std::string str(JNIEnv* env, jstring s) {
    if (!s) return "";
    const char* c = env->GetStringUTFChars(s, nullptr);
    std::string o = c ? c : "";
    if (c) env->ReleaseStringUTFChars(s, c);
    return o;
}

extern "C" JNIEXPORT jstring JNICALL
Java_com_plaudharness_speechbench_NativeBench_run(JNIEnv* env, jclass, jstring jengine, jstring jwav, jstring jopts) {
    const std::string engine = str(env, jengine), wav = str(env, jwav), opts = str(env, jopts);
    char* err = nullptr;
    char* res = nullptr;
    if (engine == "nemo-asr") res = sb_nemo_asr(wav.c_str(), opts.c_str(), &err);
    else if (engine == "nemo-diar") res = sb_nemo_diar(wav.c_str(), opts.c_str(), &err);
    else if (engine == "whisper") res = sb_whisper_asr(wav.c_str(), opts.c_str(), &err);
    else if (engine == "sherpa-diar") res = sb_sherpa_diar(wav.c_str(), opts.c_str(), &err);
    if (!res) {
        std::string msg = "speechbench " + engine + ": " + (err ? err : "unknown engine");
        std::free(err);
        env->ThrowNew(env->FindClass("java/lang/RuntimeException"), msg.c_str());
        return nullptr;
    }
    jstring out = env->NewStringUTF(res);
    std::free(res);
    return out;
}
