package com.plaudharness.speechbench

import android.app.Activity
import android.graphics.Typeface
import android.os.Bundle
import android.util.Log
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Spinner
import android.widget.TextView
import org.json.JSONObject
import java.io.File

/**
 * SpeechBench: the two local stacks of docs/nvidia-speech.md on this phone.
 *
 *  - NVIDIA: Nemotron 3.5 ASR with Nemotron 3 Diarization tagging each word (the
 *    runtime's own integration), or ASR + diarization run separately and joined by
 *    the harness rule.
 *  - Current stack: whisper.cpp small.en + sherpa-onnx diarization, joined by the
 *    harness rule (Transcript.assign).
 *
 * Files live in the app's external files dir (no permission needed):
 *   models/  nemotron-3.5-asr-streaming-0.6b.q8_0.gguf, Nemotron-3-Diarization.q8_0.gguf,
 *            ggml-small.en-q8_0.bin, segmentation-3.0.onnx, campplus.onnx
 *   audio/   16 kHz mono WAVs
 *   results/ one JSON per run
 *
 * Scripted use (research/nvidia/android/bench.sh):
 *   am start -n com.plaudharness.speechbench/.MainActivity --es suite nvidia|current|<engine>
 *            --es wav <name in audio/> [--es opts "k=v;..."] [--es out <name>]
 * logs "SPEECHBENCH_DONE <json path>" or "SPEECHBENCH_ERROR <message>".
 */
class MainActivity : Activity() {
    private lateinit var status: TextView
    private lateinit var output: TextView
    private lateinit var wavs: Spinner
    private val tag = "SPEECHBENCH"

    private val dir by lazy { getExternalFilesDir(null)!! }
    private fun model(name: String) = File(dir, "models/$name").absolutePath
    private val nemoAsr get() = model("nemotron-3.5-asr-streaming-0.6b.q8_0.gguf")
    private val nemoDiar get() = model("Nemotron-3-Diarization.q8_0.gguf")
    private val whisper get() = model("ggml-small.en-q8_0.bin")
    private val segmentation get() = model("segmentation-3.0.onnx")
    private val embedding get() = model("campplus.onnx")

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        File(dir, "models").mkdirs(); File(dir, "audio").mkdirs(); File(dir, "results").mkdirs()
        val root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(24, 24, 24, 24) }
        status = TextView(this).apply { text = "Pick a WAV (16 kHz mono) and a stack." }
        wavs = Spinner(this)
        val files = File(dir, "audio").listFiles { f -> f.name.endsWith(".wav") }?.map { it.name }?.sorted() ?: emptyList()
        wavs.adapter = ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item, files)
        root.addView(status); root.addView(wavs)
        fun button(label: String, suite: String) = Button(this).apply {
            text = label
            setOnClickListener { start(suite, wavs.selectedItem as? String ?: return@setOnClickListener, "", "") }
        }
        root.addView(button("NVIDIA: Nemotron ASR + speaker tags", "nvidia"))
        root.addView(button("NVIDIA: ASR + diarization, harness rule", "nvidia-separate"))
        root.addView(button("Current: whisper.cpp + sherpa-onnx", "current"))
        output = TextView(this).apply { typeface = Typeface.MONOSPACE; textSize = 12f }
        root.addView(ScrollView(this).apply { addView(output) }, LinearLayout.LayoutParams(-1, 0, 1f))
        setContentView(root)

        intent.getStringExtra("suite")?.let { suite ->
            start(suite, intent.getStringExtra("wav") ?: "", intent.getStringExtra("opts") ?: "", intent.getStringExtra("out") ?: "")
        }
    }

    private fun start(suite: String, wavName: String, extraOpts: String, outName: String) {
        val wav = if (wavName.startsWith("/")) wavName else File(dir, "audio/$wavName").absolutePath
        status.text = "Running $suite on ${File(wav).name} ..."
        Thread {
            try {
                val (text, doc) = runSuite(suite, wav, extraOpts)
                val out = File(dir, "results/" + outName.ifEmpty { "${File(wav).nameWithoutExtension}.$suite.json" })
                out.writeText(doc.toString(1))
                Log.i(tag, "SPEECHBENCH_DONE ${out.absolutePath}")
                runOnUiThread { status.text = "Done: ${out.name}"; output.text = text }
            } catch (t: Throwable) {
                Log.e(tag, "SPEECHBENCH_ERROR ${t.message}", t)
                runOnUiThread { status.text = "Error: ${t.message}" }
            }
        }.start()
    }

    private fun engine(name: String, wav: String, opts: String): JSONObject {
        Log.i(tag, "engine $name opts=$opts")
        return JSONObject(NativeBench.run(name, wav, opts))
    }

    private fun summary(label: String, r: JSONObject): String {
        val t = r.getJSONObject("timing")
        val m = r.getJSONObject("memory")
        return "%-14s load %6.2f s  run %7.2f s  RTF %.3f  peak %5.0f MB".format(
            label, t.getDouble("load_s"), t.getDouble("process_s"), t.getDouble("rtf"), m.getLong("peak_rss_bytes") / 1048576.0)
    }

    /** Returns the text to show and the JSON document to save. */
    private fun runSuite(suite: String, wav: String, extra: String): Pair<String, JSONObject> {
        val doc = JSONObject().put("suite", suite).put("wav", wav)
            .put("device", android.os.Build.MANUFACTURER + " " + android.os.Build.MODEL)
            .put("abi", android.os.Build.SUPPORTED_ABIS.joinToString(",")).put("sdk", android.os.Build.VERSION.SDK_INT)
        fun join(vararg kv: String) = (kv.toList() + listOf(extra)).filter { it.isNotEmpty() }.joinToString(";")
        return when (suite) {
            "nvidia" -> {
                val r = engine("nemo-asr", wav, join("model=$nemoAsr", "diar_model=$nemoDiar", "gpu=-1"))
                doc.put("results", JSONObject().put("nemo-asr+tags", r))
                val lines = Transcript.lines(Transcript.words(r))
                Pair(summary("nemotron+tags", r) + "\n\n" + Transcript.render(lines), doc)
            }
            "nvidia-separate" -> {
                val a = engine("nemo-asr", wav, join("model=$nemoAsr", "gpu=-1"))
                val d = engine("nemo-diar", wav, join("diar_model=$nemoDiar", "gpu=-1"))
                doc.put("results", JSONObject().put("nemo-asr", a).put("nemo-diar", d))
                val lines = Transcript.lines(Transcript.assign(Transcript.words(a), Transcript.turns(d)))
                Pair(summary("nemotron-asr", a) + "\n" + summary("nemotron-diar", d) + "\n\n" + Transcript.render(lines), doc)
            }
            "current" -> {
                val a = engine("whisper", wav, join("model=$whisper", "threads=4"))
                val d = engine("sherpa-diar", wav, join("segmentation=$segmentation", "embedding=$embedding", "threshold=1.15"))
                doc.put("results", JSONObject().put("whisper", a).put("sherpa-diar", d))
                val lines = Transcript.lines(Transcript.assign(Transcript.words(a), Transcript.turns(d)))
                Pair(summary("whisper.cpp", a) + "\n" + summary("sherpa-onnx", d) + "\n\n" + Transcript.render(lines), doc)
            }
            else -> {  // one engine, options entirely from the caller
                val r = engine(suite, wav, extra)
                doc.put("results", JSONObject().put(suite, r))
                Pair(summary(suite, r), doc)
            }
        }
    }
}
