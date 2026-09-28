package com.plaud.template.debug

import android.app.Activity
import android.os.Bundle
import android.util.Log
import com.tinnotech.penblesdk.entity.BleDevice
import com.tinnotech.penblesdk.entity.BleFile
import sdk.PlaudDeviceAgent
import sdk.PlaudDeviceAgentListener
import sdk.audio.AudioExportFormat
import sdk.audio.AudioExporter
import sdk.audio.ExportStage
import java.io.File
import java.security.MessageDigest
import java.util.TreeMap

/**
 * R7-S13 recording-pull runtime driver (DEBUG-ONLY, not part of the product UI).
 *
 * Purpose: make the GENUINE official SDK pull a recording off our own Bumble
 * emulator over netsim, using ONLY public SDK entry points, and capture what
 * the SDK hands the application so it can be compared byte-for-byte with what
 * the peripheral served. Two public paths are driven:
 *
 *   mode=raw     PlaudDeviceAgent.syncFile(sessionId, 0, 0) — the "collector"
 *                path: bleSyncFileHead / bleData(sessionId, offset, bytes) /
 *                bleSyncFileTail / bleDataComplete. The docs say the collector
 *                "receives exactly what the device sends"; the bytes are
 *                reassembled by offset here and hashed.
 *   mode=export  PlaudDeviceAgent.exportAudio(sessionId, dir, OPUS, 1, cb) —
 *                the product path (download + decode + re-encode), reporting
 *                ExportStage DOWNLOADING -> TRANSCODING; the output file is
 *                hashed.
 *   mode=both    raw, then export (default).
 *
 * The connection is established exactly as in K3CaptureActivity (recovery
 * entry point with a SYNTHETIC historical id; the emulator advertises
 * portVersion 7 so no cloud material is involved). After bleBind(status=0)
 * the driver calls getFileList() and pulls the FIRST listed file.
 *
 *   adb shell am start -n com.plaud.template/.debug.PullCaptureActivity \
 *       --es id "SYNTH-HIST-0001" --es mode both
 *
 * Every event is mirrored to logcat with a PULLCAP_ prefix; the reassembled
 * raw bytes are written to filesDir/pullcap-raw-<sessionId>.bin and the export
 * output to filesDir/pullcap-export/ so `adb shell run-as` can pull them.
 */
class PullCaptureActivity : Activity() {

    private val tag = "PULLCAP"
    @Volatile private var fired = false
    @Volatile private var listed = false
    @Volatile private var mode = "both"
    @Volatile private var stopAfterRaw = false
    @Volatile private var target: BleFile? = null

    // raw-path accumulator: offset -> bytes (TreeMap keeps offset order)
    private val chunks = TreeMap<Long, ByteArray>()
    @Volatile private var rawSession: Long = 0
    @Volatile private var rawDoneAt: Long = 0
    @Volatile private var dataFrames = 0
    @Volatile private var firstDataAt: Long = 0
    @Volatile private var rawFinished = false
    @Volatile private var rawTrigger = ""
    private val tailHandler = android.os.Handler(android.os.Looper.getMainLooper())

    private fun sha256(bytes: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }

    private fun sha256(file: File): String = sha256(file.readBytes())

    private fun now() = System.currentTimeMillis()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val id = intent.getStringExtra("id") ?: "SYNTH-HIST-0001"
        mode = intent.getStringExtra("mode") ?: "both"
        stopAfterRaw = intent.getBooleanExtra("stop", false)
        Log.i(tag, "PULLCAP_START id=\"$id\" mode=$mode stopAfterRaw=$stopAfterRaw")

        PlaudDeviceAgent.listener = object : PlaudDeviceAgentListener {
            override fun bleScanResult(devices: List<BleDevice>) {
                if (fired) return
                val dev = devices.firstOrNull() ?: return
                fired = true
                Log.i(tag, "PULLCAP_SCAN sn=${dev.getSerialNumber()}")
                try { PlaudDeviceAgent.stopScan() } catch (e: Exception) { Log.w(tag, "stopScan threw", e) }
                try {
                    PlaudDeviceAgent.recoveryConnectBleDevice(dev, id)
                } catch (e: Exception) {
                    Log.e(tag, "PULLCAP_RECOVERY_THREW", e)
                }
            }

            override fun bleConnectState(state: Int) {
                Log.i(tag, "PULLCAP_CONNECT_STATE state=$state t=${now()}")
            }

            override fun bleConnectStage(sn: String?, stage: String, detail: String?) {
                Log.i(tag, "PULLCAP_STAGE stage=\"$stage\" detail=\"$detail\"")
            }

            override fun bleBind(sn: String?, status: Int, protVersion: Int, timezone: Int) {
                Log.i(tag, "PULLCAP_BIND sn=\"$sn\" status=$status protVersion=$protVersion tz=$timezone")
                if (status == 0 && !listed) {
                    listed = true
                    try {
                        Log.i(tag, "PULLCAP_GETFILELIST t=${now()}")
                        PlaudDeviceAgent.getFileList()
                    } catch (e: Exception) {
                        Log.e(tag, "PULLCAP_GETFILELIST_THREW", e)
                    }
                }
            }

            override fun bleFileList(files: List<BleFile>) {
                Log.i(tag, "PULLCAP_FILELIST n=${files.size} t=${now()}")
                for (f in files) {
                    Log.i(tag, "PULLCAP_FILE ${describe(f)}")
                }
                if (target != null) return
                val f = files.firstOrNull() ?: return
                target = f
                when (mode) {
                    "raw" -> startRaw(f)
                    "export" -> startExport(f)
                    else -> startRaw(f)
                }
            }

            override fun bleSyncFileHead(sessionId: Long, status: Int) {
                Log.i(tag, "PULLCAP_HEAD sessionId=$sessionId status=$status t=${now()}")
            }

            override fun bleData(sessionId: Long, offset: Long, data: ByteArray) {
                if (firstDataAt == 0L) firstDataAt = now()
                dataFrames++
                synchronized(chunks) { chunks[offset] = data.copyOf() }
                if (dataFrames <= 3 || dataFrames % 50 == 0) {
                    Log.i(tag, "PULLCAP_DATA sessionId=$sessionId offset=$offset len=${data.size} n=$dataFrames t=${now()}")
                }
            }

            override fun bleSyncFileTail(sessionId: Long, status: Int) {
                Log.i(tag, "PULLCAP_TAIL sessionId=$sessionId status=$status t=${now()}")
                // Run 1 showed the TAIL arriving with every DATA frame present but no
                // bleDataComplete within 60 s. Finalise from the TAIL after a grace
                // period so the bytes can be compared; record which event won.
                tailHandler.postDelayed({
                    if (!rawFinished) { rawTrigger = "TAIL+3s(no bleDataComplete)"; finishRaw() }
                }, 3000)
            }

            override fun bleDataComplete() {
                rawDoneAt = now()
                Log.i(tag, "PULLCAP_DATA_COMPLETE frames=$dataFrames t=$rawDoneAt firstDataAt=$firstDataAt")
                if (!rawFinished) { rawTrigger = "bleDataComplete"; finishRaw() }
            }

            override fun bleSyncFileStop() {
                Log.i(tag, "PULLCAP_SYNC_STOP t=${now()}")
            }
        }

        // BLANK ON PURPOSE (R7-S14 D7). PlaudDeviceAgent.initSDK -> NiceBuildSdk.initSdk
        // stores a non-blank token and then immediately POSTs partner/sdk/gen-key to
        // Plaud's server ("Partner API: Token 可用，正在获取 RSA 密钥对...",
        // build/evidence/javap/sdk/NiceBuildSdk.txt initSdk, isBlank branch). Earlier
        // runs passed a synthetic JWT here and so sent that request once per app start;
        // every one was rejected with 401. A blank token takes the clearPartnerData()
        // branch instead, and nothing on the legacy recovery path needs a token.
        val initToken = ""
        try {
            PlaudDeviceAgent.initSDK(applicationContext, initToken, "api.plaud.ai")
            PlaudDeviceAgent.setBleLogLevel(Log.INFO)
        } catch (e: Exception) {
            Log.e(tag, "PULLCAP_INIT_THREW", e)
        }
        try {
            PlaudDeviceAgent.startScan()
            Log.i(tag, "PULLCAP_SCAN_STARTED")
        } catch (e: Exception) {
            Log.e(tag, "PULLCAP_SCAN_THREW", e)
        }
    }

    /** Reflective dump of a BleFile so we do not depend on its accessor names. */
    private fun describe(f: BleFile): String {
        val sb = StringBuilder()
        for (m in f.javaClass.methods) {
            val n = m.name
            if (m.parameterCount == 0 && (n.startsWith("get") || n.startsWith("is")) &&
                n != "getClass" && m.returnType != Void.TYPE) {
                try { sb.append(n).append('=').append(m.invoke(f)).append(' ') } catch (_: Exception) {}
            }
        }
        return sb.toString().trim()
    }

    private fun sessionIdOf(f: BleFile): Long {
        // BleFile exposes the recording session id through a getter; resolve it
        // reflectively (candidates in order) so the driver stays source-only.
        for (name in listOf("getSessionId", "getSession", "getSessionID", "getId")) {
            try {
                val m = f.javaClass.getMethod(name)
                val v = m.invoke(f)
                if (v is Number) return v.toLong()
            } catch (_: Exception) {}
        }
        throw IllegalStateException("no session id getter on BleFile: ${describe(f)}")
    }

    private fun startRaw(f: BleFile) {
        val sid = sessionIdOf(f)
        rawSession = sid
        synchronized(chunks) { chunks.clear() }
        dataFrames = 0
        firstDataAt = 0
        rawFinished = false
        rawTrigger = ""
        Log.i(tag, "PULLCAP_SYNCFILE sessionId=$sid start=0 end=0 t=${now()}")
        try {
            PlaudDeviceAgent.syncFile(sid, 0L, 0L)
        } catch (e: Exception) {
            Log.e(tag, "PULLCAP_SYNCFILE_THREW", e)
        }
    }

    private fun finishRaw() {
        rawFinished = true
        val (bytes, contiguous, gaps) = synchronized(chunks) {
            var expected = 0L
            var contiguous = true
            var gaps = 0
            val out = java.io.ByteArrayOutputStream()
            for ((off, data) in chunks) {
                if (off != expected) { contiguous = false; gaps++ }
                out.write(data)
                expected = off + data.size
            }
            Triple(out.toByteArray(), contiguous, gaps)
        }
        val file = File(filesDir, "pullcap-raw-$rawSession.bin")
        file.writeBytes(bytes)
        Log.i(tag, "PULLCAP_RAW_DONE sessionId=$rawSession len=${bytes.size} frames=$dataFrames contiguous=$contiguous gaps=$gaps sha256=${sha256(bytes)} trigger=\"$rawTrigger\" path=${file.absolutePath}")
        if (stopAfterRaw) {
            // Run 5: does the public collector path expect the app to close the
            // session with stopSyncFile()? Observe the SDK's op-queue reaction.
            try {
                Log.i(tag, "PULLCAP_STOPSYNC_CALL t=${now()}")
                PlaudDeviceAgent.stopSyncFile()
            } catch (e: Exception) { Log.e(tag, "PULLCAP_STOPSYNC_THREW", e) }
        }
        if (mode == "both") {
            tailHandler.postDelayed({ target?.let { startExport(it) } }, if (stopAfterRaw) 2000 else 0)
        }
    }

    private fun startExport(f: BleFile) {
        val sid = sessionIdOf(f)
        val dir = File(filesDir, "pullcap-export").apply { mkdirs() }
        Log.i(tag, "PULLCAP_EXPORT sessionId=$sid format=OPUS channels=1 t=${now()}")
        try {
            PlaudDeviceAgent.exportAudio(sid, dir, AudioExportFormat.OPUS, 1, object : AudioExporter.ExportCallback {
                override fun onProgress(progress: Int, message: String) {
                    if (progress % 25 == 0 || progress >= 99) Log.i(tag, "PULLCAP_EXPORT_PROGRESS p=$progress msg=\"$message\" t=${now()}")
                }
                override fun onStageChanged(stage: ExportStage) {
                    Log.i(tag, "PULLCAP_EXPORT_STAGE stage=$stage t=${now()}")
                }
                override fun onComplete(outputFile: File) {
                    Log.i(tag, "PULLCAP_EXPORT_DONE path=${outputFile.absolutePath} len=${outputFile.length()} sha256=${sha256(outputFile)} t=${now()}")
                }
                override fun onError(error: String) {
                    Log.e(tag, "PULLCAP_EXPORT_ERROR \"$error\" t=${now()}")
                }
            })
        } catch (e: Exception) {
            Log.e(tag, "PULLCAP_EXPORT_THREW", e)
        }
    }
}
