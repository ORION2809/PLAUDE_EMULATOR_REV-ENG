// speechbench: the C entry points of the three engine libraries (docs/nvidia-speech.md).
// Each returns a malloc'd JSON document (schema plaud-harness/speechbench/1) or
// NULL with *err set to a malloc'd message; release both with free().
#pragma once
#include <stddef.h>
#ifdef __cplusplus
extern "C" {
#endif
char* sb_nemo_asr(const char* wav_path, const char* options, char** err);
char* sb_nemo_diar(const char* wav_path, const char* options, char** err);
char* sb_whisper_asr(const char* wav_path, const char* options, char** err);
char* sb_sherpa_diar(const char* wav_path, const char* options, char** err);
/* Live NVIDIA ASR session (streaming API, bounded memory): create, push audio as it is
   recorded (returns the live text so far), finish (returns the result JSON and frees the
   session) or cancel. */
void* sb_nemo_session_create(const char* options, char** err);
char* sb_nemo_session_push(void* session, const float* samples, size_t n, int sample_rate, char** err);
char* sb_nemo_session_finish(void* session, char** err);
void sb_nemo_session_cancel(void* session);
#ifdef __cplusplus
}
#endif
