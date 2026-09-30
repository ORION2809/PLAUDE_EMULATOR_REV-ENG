@preconcurrency import AVFoundation
import CoreML
import FluidAudio
import Foundation

/// Where Core ML runs a model. `.ane` is FluidAudio's default for the ASR encoder
/// (`.cpuAndNeuralEngine`); `.cpu` is what FluidAudio recommends on iPhones to avoid the
/// Neural Engine residency limit; `.all` lets Core ML choose (and may pick the GPU,
/// which iOS forbids in the background).
public enum ComputeTarget: String, Sendable, CaseIterable {
    case ane, cpu, gpu, all

    public var units: MLComputeUnits {
        switch self {
        case .ane: return .cpuAndNeuralEngine
        case .cpu: return .cpuOnly
        case .gpu: return .cpuAndGPU
        case .all: return .all
        }
    }
}

/// Nemotron-EN streaming chunk (FluidAudio publishes these three).
public enum NemotronChunk: Int, Sendable, CaseIterable {
    case ms560 = 560, ms1120 = 1120, ms2240 = 2240

    var fluid: NemotronChunkSize {
        switch self {
        case .ms560: return .ms560
        case .ms1120: return .ms1120
        case .ms2240: return .ms2240
        }
    }
}

public struct PipelineConfig: Sendable {
    /// Nemotron-EN chunk: 1120 ms is NVIDIA's trained [70,13] setting (1.12 s latency).
    public var asrChunk: NemotronChunk = .ms1120
    public var asrCompute: ComputeTarget = .ane
    /// A FluidAudio `Nemotron3Config` preset name (fast128, c128-split-w8a8, fast32, offline, ...).
    public var diarPreset: String = "fast128"
    public var diarCompute: ComputeTarget = .ane
    /// Speaker-activity threshold and minimum segment length (FluidAudio's defaults).
    public var diarThreshold: Float = 0.5
    public var diarMinDuration: Float = 0.2
    /// Root folder for downloaded models (nil = FluidAudio's Application Support default).
    public var modelsDirectory: URL?

    public init() {}
}

/// Timing and memory of one run, for the benchmark JSON.
public struct RunStats: Codable, Sendable {
    public var audioSeconds: Double = 0
    public var asrLoadSeconds: Double = 0
    public var asrSeconds: Double = 0
    public var diarLoadSeconds: Double = 0
    public var diarSeconds: Double = 0
    public var peakResidentBytes: Int64 = 0
    public var peakFootprintBytes: Int64 = 0
}

/// Record-then-transcribe pipeline: streaming ASR while audio arrives (live partial text),
/// diarization after the recording stops, then the harness's word-to-speaker rule.
public actor SpeechPipeline {
    public let config: PipelineConfig
    private var asr: StreamingNemotronAsrManager?
    private var diarizer: Nemotron3Diarizer?
    private var diarConfig: Nemotron3Config
    private var probabilities: [Float] = []
    private var frameCount = 0
    public private(set) var stats = RunStats()

    public init(config: PipelineConfig) throws {
        guard let preset = Nemotron3Config.preset(named: config.diarPreset) else {
            throw MiviSpeechError.unknownPreset(config.diarPreset)
        }
        self.config = config
        self.diarConfig = preset
    }

    /// Download (first run) and load both models.
    public func load(progress: (@Sendable (String) -> Void)? = nil) async throws {
        var t0 = Date()
        let mlc = MLModelConfiguration()
        mlc.computeUnits = config.asrCompute.units
        let asr = StreamingNemotronAsrManager(configuration: mlc, requestedChunkSize: config.asrChunk.fluid)
        progress?("loading Nemotron-EN \(config.asrChunk.rawValue) ms")
        try await asr.loadModels(to: config.modelsDirectory)  // uses the configuration given to init
        self.asr = asr
        stats.asrLoadSeconds = Date().timeIntervalSince(t0)
        t0 = Date()
        progress?("loading Nemotron 3 Diarization (\(config.diarPreset))")
        let models = try await Nemotron3Models.loadFromHuggingFace(
            config: diarConfig, cacheDirectory: config.modelsDirectory, computeUnits: config.diarCompute.units)
        diarizer = Nemotron3Diarizer(config: diarConfig, models: models)
        stats.diarLoadSeconds = Date().timeIntervalSince(t0)
        Memory.sample(into: &stats)
    }

    /// Start a new recording.
    public func begin() async {
        await asr?.reset()
        diarizer?.reset()
        probabilities.removeAll()
        frameCount = 0
        stats.audioSeconds = 0
        stats.asrSeconds = 0
        stats.diarSeconds = 0
    }

    /// Feed audio as it is recorded (any rate/channels; resampled to 16 kHz mono).
    /// Speech recognition runs as each 1.12 s chunk completes; diarization features are
    /// buffered and computed now too, so stopping only has to flush the tail.
    public func append(_ buffer: sending AVAudioPCMBuffer) async throws {
        guard let asr, let diarizer else { throw MiviSpeechError.notLoaded }
        let mono = try Audio.mono16k(buffer)  // before the buffer is handed to the ASR actor
        stats.audioSeconds += Double(mono.count) / 16_000
        let t0 = Date()
        _ = try await asr.process(audioBuffer: buffer)
        stats.asrSeconds += Date().timeIntervalSince(t0)
        let t1 = Date()
        diarizer.appendAudio(mono)
        for r in try diarizer.processBufferedAudio() {
            probabilities.append(contentsOf: r.probabilities)
            frameCount += r.frameCount
        }
        stats.diarSeconds += Date().timeIntervalSince(t1)
        Memory.sample(into: &stats)
    }

    /// The live transcript so far (no speakers yet).
    public func partial() async -> String { await asr?.getPartialTranscript() ?? "" }

    /// Stop: finish both models and return the speaker-attributed transcript.
    public func finish() async throws -> Transcript {
        guard let asr, let diarizer else { throw MiviSpeechError.notLoaded }
        var t0 = Date()
        let (_, timings) = try await asr.finishWithTokenTimings()
        stats.asrSeconds += Date().timeIntervalSince(t0)
        let words = buildWordTimings(from: timings).map { Word(w: $0.word, start: $0.startTime, end: $0.endTime) }
        t0 = Date()
        for r in try diarizer.finishStream() {
            probabilities.append(contentsOf: r.probabilities)
            frameCount += r.frameCount
        }
        let segments = Nemotron3Diarizer.segments(
            probabilities: probabilities, frameCount: frameCount, numSpeakers: diarConfig.numSpeakers,
            threshold: config.diarThreshold, minDurationSeconds: config.diarMinDuration)
        stats.diarSeconds += Date().timeIntervalSince(t0)
        Memory.sample(into: &stats)
        let turns = segments.map { Turn(start: Double($0.startSeconds), end: Double($0.endSeconds), speaker: $0.speakerIndex + 1) }
        return Transcript(words: words, turns: turns)
    }

    /// Whole-file convenience for evaluation: feed a file in 1 s buffers, then finish.
    public func transcribe(file url: URL) async throws -> Transcript {
        await begin()
        let file = try AVAudioFile(forReading: url)
        let format = file.processingFormat
        let step = AVAudioFrameCount(format.sampleRate)
        while file.framePosition < file.length {
            guard let buf = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: step) else { break }
            try file.read(into: buf, frameCount: step)
            if buf.frameLength == 0 { break }
            try await append(buf)
        }
        return try await finish()
    }
}

public enum MiviSpeechError: Error, CustomStringConvertible {
    case unknownPreset(String), notLoaded, audio(String)
    public var description: String {
        switch self {
        case .unknownPreset(let p): return "unknown diarization preset \(p)"
        case .notLoaded: return "models not loaded"
        case .audio(let m): return "audio: \(m)"
        }
    }
}

enum Audio {
    /// Any PCM buffer -> 16 kHz mono float samples.
    static func mono16k(_ buffer: AVAudioPCMBuffer) throws -> [Float] {
        let target = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16_000, channels: 1, interleaved: false)!
        if buffer.format.sampleRate == 16_000, buffer.format.channelCount == 1,
           buffer.format.commonFormat == .pcmFormatFloat32, let ch = buffer.floatChannelData {
            return Array(UnsafeBufferPointer(start: ch[0], count: Int(buffer.frameLength)))
        }
        guard let conv = AVAudioConverter(from: buffer.format, to: target) else { throw MiviSpeechError.audio("no converter") }
        let cap = AVAudioFrameCount(Double(buffer.frameLength) * 16_000 / buffer.format.sampleRate) + 32
        guard let out = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: cap) else { throw MiviSpeechError.audio("alloc") }
        var fed = false
        var err: NSError?
        conv.convert(to: out, error: &err) { _, status in
            if fed { status.pointee = .noDataNow; return nil }
            fed = true
            status.pointee = .haveData
            return buffer
        }
        if let err { throw err }
        return Array(UnsafeBufferPointer(start: out.floatChannelData![0], count: Int(out.frameLength)))
    }
}

enum Memory {
    /// Peak resident set (getrusage) and the current physical footprint (task_vm_info,
    /// the figure iOS jetsam limits), keeping the maximum seen.
    static func sample(into s: inout RunStats) {
        var ru = rusage()
        getrusage(RUSAGE_SELF, &ru)
        s.peakResidentBytes = max(s.peakResidentBytes, Int64(ru.ru_maxrss))
        var info = task_vm_info_data_t()
        var count = mach_msg_type_number_t(MemoryLayout<task_vm_info_data_t>.size / MemoryLayout<natural_t>.size)
        let kr = withUnsafeMutablePointer(to: &info) {
            $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
                task_info(mach_task_self_, task_flavor_t(TASK_VM_INFO), $0, &count)
            }
        }
        if kr == KERN_SUCCESS { s.peakFootprintBytes = max(s.peakFootprintBytes, Int64(info.phys_footprint)) }
    }
}
