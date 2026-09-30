package com.plaudharness.speechbench

/**
 * The native speechbench layer (research/nvidia/android/native): one call runs one
 * engine on one 16 kHz mono WAV and returns its JSON result
 * (schema plaud-harness/speechbench/1: timing, peak memory, words, turns).
 *
 * Engines: "nemo-asr" (NVIDIA Nemotron ASR; with diar_model= its words carry
 * Sortformer speaker tags), "nemo-diar" (Nemotron 3 Diarization), "whisper"
 * (whisper.cpp small.en), "sherpa-diar" (sherpa-onnx segmentation-3.0 + CAM++).
 * Options are "key=value;key=value" (see the engine sources).
 */
object NativeBench {
    init {
        System.loadLibrary("speechbench")
    }

    @JvmStatic
    external fun run(engine: String, wavPath: String, options: String): String
}
