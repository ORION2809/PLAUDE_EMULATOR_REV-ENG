import Foundation
import MiviSpeechCore

// mivi-speech: run the Apple engine on a recording (docs/nvidia-speech.md).
//
//   mivi-speech transcribe <audio> [--asr-chunk 560|1120|2240] [--asr-compute ane|cpu|gpu|all]
//                                   [--diar <preset>] [--diar-compute ane|cpu|gpu|all]
//                                   [--models DIR] [--out result.json] [--srt out.srt] [--quiet]
//
// Prints the speaker-attributed transcript and writes a plaud-harness/speechbench/1 JSON
// (timing, memory, words with speakers, turns) that research/nvidia scripts score.

func usage() -> Never {
    FileHandle.standardError.write(Data("""
    usage: mivi-speech transcribe <audio> [--asr-chunk 1120] [--asr-compute ane] [--diar fast128]
                                          [--diar-compute ane] [--models DIR] [--out result.json]
                                          [--srt out.srt] [--quiet]

    """.utf8))
    exit(2)
}

func target(_ s: String) -> ComputeTarget {
    guard let t = ComputeTarget(rawValue: s) else { usage() }
    return t
}

var args = Array(CommandLine.arguments.dropFirst())
guard args.first == "transcribe", args.count >= 2 else { usage() }
let input = URL(fileURLWithPath: args[1])
args.removeFirst(2)
var config = PipelineConfig()
var outPath: String?
var srtPath: String?
var quiet = false
while !args.isEmpty {
    let flag = args.removeFirst()
    func value() -> String {
        guard !args.isEmpty else { usage() }
        return args.removeFirst()
    }
    switch flag {
    case "--asr-chunk":
        guard let c = NemotronChunk(rawValue: Int(value()) ?? 0) else { usage() }
        config.asrChunk = c
    case "--asr-compute": config.asrCompute = target(value())
    case "--diar": config.diarPreset = value()
    case "--diar-compute": config.diarCompute = target(value())
    case "--models": config.modelsDirectory = URL(fileURLWithPath: value(), isDirectory: true)
    case "--out": outPath = value()
    case "--srt": srtPath = value()
    case "--quiet": quiet = true
    default: usage()
    }
}

func log(_ s: String) { if !quiet { FileHandle.standardError.write(Data("[mivi-speech] \(s)\n".utf8)) } }

do {
    let pipeline = try SpeechPipeline(config: config)
    try await pipeline.load(progress: { log($0) })
    log("transcribing \(input.lastPathComponent)")
    let transcript = try await pipeline.transcribe(file: input)
    let s = await pipeline.stats
    let process = s.asrSeconds + s.diarSeconds
    log(String(format: "audio %.1f s  asr %.2f s  diar %.2f s  RTF %.3f  peak RSS %.0f MB  footprint %.0f MB",
               s.audioSeconds, s.asrSeconds, s.diarSeconds, process / max(s.audioSeconds, 1e-9),
               Double(s.peakResidentBytes) / 1_048_576, Double(s.peakFootprintBytes) / 1_048_576))
    if !quiet { print(transcript.text()) }
    if let srtPath { try transcript.srt().write(toFile: srtPath, atomically: true, encoding: .utf8) }
    if let outPath {
        let doc: [String: Any] = [
            "schema": "plaud-harness/speechbench/1",
            "engine": "fluidaudio-nemotron",
            "settings": [
                "asr_model": "nemotron-speech-streaming-en-0.6b (FluidAudio Core ML, int8 encoder)",
                "asr_chunk_ms": config.asrChunk.rawValue, "asr_compute": config.asrCompute.rawValue,
                "diar_model": "Nemotron-3-Diarization (FluidAudio Core ML)", "diar_preset": config.diarPreset,
                "diar_compute": config.diarCompute.rawValue, "diar_threshold": config.diarThreshold,
                "diar_min_duration_s": config.diarMinDuration, "fluidaudio": "0.17.4",
                "assignment": "harness rule (max overlap; ties to earliest-starting turn; else nearest turn)",
            ],
            "timing": [
                "audio_s": s.audioSeconds, "load_s": s.asrLoadSeconds + s.diarLoadSeconds,
                "asr_load_s": s.asrLoadSeconds, "diar_load_s": s.diarLoadSeconds,
                "asr_s": s.asrSeconds, "diar_s": s.diarSeconds, "process_s": process,
                "rtf": process / max(s.audioSeconds, 1e-9),
            ],
            "memory": ["peak_rss_bytes": s.peakResidentBytes, "peak_footprint_bytes": s.peakFootprintBytes],
            "words": transcript.words.map { ["w": $0.w, "start": $0.start, "end": $0.end, "speaker": $0.speaker] },
            "turns": transcript.turns.map { ["start": $0.start, "end": $0.end, "speaker": $0.speaker] },
        ]
        let data = try JSONSerialization.data(withJSONObject: doc, options: [.sortedKeys])
        try data.write(to: URL(fileURLWithPath: outPath))
        log("wrote \(outPath)")
    }
} catch {
    FileHandle.standardError.write(Data("mivi-speech: \(error)\n".utf8))
    exit(1)
}
