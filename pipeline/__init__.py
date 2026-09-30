"""plaud-harness ``pipeline``: the ASR/diarization stack under test.

Layer 2 (generator) writes a meeting directory; a Pipeline here turns its
audio into a Hypothesis (``plaud-harness/hypothesis/1``); Layer 3 (evals)
scores it.  See docs/pipeline.md for the interfaces, which entries are real
systems versus harness oracles, and the V5 procedure.

Registered names (``python -m pipeline list``):

  oracle, perturbed-oracle      harness self-tests (NOT systems under test)
  energy-vad-cluster            model-free diarizer, no ASR (runs here)
  whisper-sherpa                faster-whisper small.en + sherpa-onnx diarization,
                                open ungated models on CPU (runs here once
                                ``python -m pipeline fetch-models`` has run)
  sherpa-onnx-diarization       the same diarizer alone (DER/JER only)
  faster-whisper                faster-whisper + the model-free diarizer
  whisper-sherpa-ecapa          whisper-sherpa's transcript + ECAPA embedding-cluster turns cut
                                into sherpa-onnx's speaker count (needs torch + SpeechBrain;
                                a separate environment, docs/pipeline.md §11.10)
  pyannote-audio, faster-whisper+pyannote
                                pyannote.audio adapters; not in .venv, run in a separate
                                environment against pyannote.audio 4.0.7 / community-1 (§11.11)
  embedding-cluster             SpeechBrain ECAPA embeddings + the model-free clustering; not
                                in .venv, run in a torch + SpeechBrain environment (§11.9)
  whisperx                      optional adapter, UNTESTED here (package absent)
"""

from .base import (
    DEVICE_SAMPLE_RATE_HZ,
    HYPOTHESIS_SCHEMA,
    MEETING_SCHEMA,
    AudioFormatError,
    ComposedPipeline,
    Diarizer,
    Hypothesis,
    LoadedAudio,
    ParamError,
    Pipeline,
    PipelineConfig,
    PipelineError,
    PipelineUnavailable,
    Transcriber,
    UnknownPipeline,
    describe_registry,
    get_pipeline,
    load_audio,
    register,
    registered,
)

__version__ = "0.1.0"

__all__ = [
    "DEVICE_SAMPLE_RATE_HZ",
    "HYPOTHESIS_SCHEMA",
    "MEETING_SCHEMA",
    "AudioFormatError",
    "ComposedPipeline",
    "Diarizer",
    "Hypothesis",
    "LoadedAudio",
    "ParamError",
    "Pipeline",
    "PipelineConfig",
    "PipelineError",
    "PipelineUnavailable",
    "Transcriber",
    "UnknownPipeline",
    "describe_registry",
    "get_pipeline",
    "load_audio",
    "register",
    "registered",
    "__version__",
]
