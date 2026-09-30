// swift-tools-version: 6.1
// MiviSpeech: the Apple engine of the on-device speech product (docs/nvidia-speech.md).
// NVIDIA Nemotron-EN streaming ASR (1.12 s) + Nemotron 3 Diarization through FluidAudio's
// Core ML ports, with the harness's word-to-speaker rule. Builds with the Command Line
// Tools on macOS (`swift build -c release`); the same library targets iOS 17.
import PackageDescription

let package = Package(
    name: "MiviSpeech",
    platforms: [.macOS(.v14), .iOS(.v17)],
    products: [
        .library(name: "MiviSpeechCore", targets: ["MiviSpeechCore"]),
        .executable(name: "mivi-speech", targets: ["MiviSpeechCLI"]),
    ],
    dependencies: [
        // Pinned; the default NemoTextProcessing trait (an 87 MB binary target) is off.
        .package(url: "https://github.com/FluidInference/FluidAudio.git", exact: "0.17.4", traits: []),
    ],
    targets: [
        .target(
            name: "MiviSpeechCore",
            dependencies: [.product(name: "FluidAudio", package: "FluidAudio")]
        ),
        .executableTarget(
            name: "MiviSpeechCLI",
            dependencies: ["MiviSpeechCore"],
            // single-threaded top-level script code; the library keeps Swift 6 checking
            swiftSettings: [.swiftLanguageMode(.v5)]
        ),
    ]
)
