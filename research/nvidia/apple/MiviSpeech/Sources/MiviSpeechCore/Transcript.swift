import Foundation

/// One recognised word with its time span and, once assigned, its speaker (1-based; 0 = none).
public struct Word: Codable, Sendable, Equatable {
    public var w: String
    public var start: Double
    public var end: Double
    public var speaker: Int

    public init(w: String, start: Double, end: Double, speaker: Int = 0) {
        self.w = w
        self.start = start
        self.end = end
        self.speaker = speaker
    }
}

/// One diarization turn: a speaker (1-based) active from `start` to `end` seconds.
public struct Turn: Codable, Sendable, Equatable {
    public var start: Double
    public var end: Double
    public var speaker: Int

    public init(start: Double, end: Double, speaker: Int) {
        self.start = start
        self.end = end
        self.speaker = speaker
    }
}

/// A run of one speaker's consecutive words.
public struct Line: Codable, Sendable, Equatable {
    public var speaker: Int
    public var start: Double
    public var end: Double
    public var text: String
}

/// The harness's word-to-speaker rule (pipeline/base.py `assign_speakers`, tie-break
/// "floor"): a word takes the speaker with the largest total overlap; ties go to the
/// speaker of the earliest-starting overlapping turn; a word overlapping no turn takes
/// the nearest turn's speaker; with no turns every word keeps speaker 0.
public enum SpeakerAssignment {
    public static func assign(_ words: [Word], turns: [Turn]) -> [Word] {
        guard !turns.isEmpty else { return words.map { var w = $0; w.speaker = 0; return w } }
        let sorted = turns.sorted { ($0.start, $0.end) < ($1.start, $1.end) }
        return words.map { word in
            var overlap: [Int: Double] = [:]
            for t in sorted {
                let o = min(word.end, t.end) - max(word.start, t.start)
                if o > 0 { overlap[t.speaker, default: 0] += o }
            }
            var out = word
            if let best = overlap.values.max() {
                // Walk the turns in (start, end) order and take the first overlapping one whose
                // speaker is tied, exactly as SpeakerIndex does: two turns that start in the same
                // frame are then ordered by end, never by dictionary order.
                let tied = Set(overlap.filter { $0.value >= best - 1e-9 }.map(\.key))
                out.speaker = sorted.first { t in
                    tied.contains(t.speaker) && min(word.end, t.end) - max(word.start, t.start) > 0
                }!.speaker
            } else {
                out.speaker = sorted.min { gap(word, $0) < gap(word, $1) }!.speaker
            }
            return out
        }
    }

    static func gap(_ w: Word, _ t: Turn) -> Double { max(t.start - w.end, w.start - t.end, 0) }

    /// Consecutive words of one speaker, split at pauses longer than `gap` seconds.
    public static func lines(_ words: [Word], gap: Double = 1.0) -> [Line] {
        var out: [Line] = []
        var run: [Word] = []
        func flush() {
            guard let first = run.first else { return }
            out.append(Line(speaker: first.speaker, start: first.start, end: run.map(\.end).max()!,
                            text: run.map(\.w).joined(separator: " ")))
            run.removeAll()
        }
        for w in words {
            if let last = run.last, w.speaker != last.speaker || w.start - last.end > gap { flush() }
            run.append(w)
        }
        flush()
        return out
    }
}

/// The finished result of one recording.
public struct Transcript: Codable, Sendable {
    public var words: [Word]
    public var turns: [Turn]
    public var lines: [Line] { SpeakerAssignment.lines(words) }

    public init(words: [Word], turns: [Turn]) {
        self.words = SpeakerAssignment.assign(words, turns: turns)
        self.turns = turns
    }

    /// Plain text, one line per speaker run: "[m:ss Speaker n] text".
    public func text() -> String {
        lines.map { l in
            let t = Int(l.start)
            return String(format: "[%d:%02d Speaker %d] %@", t / 60, t % 60, l.speaker, l.text)
        }.joined(separator: "\n")
    }

    /// SubRip subtitles, one cue per speaker run.
    public func srt() -> String {
        func stamp(_ s: Double) -> String {
            let ms = Int((s * 1000).rounded())
            return String(format: "%02d:%02d:%02d,%03d", ms / 3_600_000, ms / 60_000 % 60, ms / 1000 % 60, ms % 1000)
        }
        return lines.enumerated().map { i, l in
            "\(i + 1)\n\(stamp(l.start)) --> \(stamp(l.end))\nSpeaker \(l.speaker): \(l.text)\n"
        }.joined(separator: "\n")
    }
}
