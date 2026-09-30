// speechbench: the C entry points of the three engine libraries (docs/nvidia-speech.md).
// Each returns a malloc'd JSON document (schema plaud-harness/speechbench/1) or
// NULL with *err set to a malloc'd message; release both with free().
#pragma once
#ifdef __cplusplus
extern "C" {
#endif
char* sb_nemo_asr(const char* wav_path, const char* options, char** err);
char* sb_nemo_diar(const char* wav_path, const char* options, char** err);
char* sb_whisper_asr(const char* wav_path, const char* options, char** err);
char* sb_sherpa_diar(const char* wav_path, const char* options, char** err);
#ifdef __cplusplus
}
#endif
