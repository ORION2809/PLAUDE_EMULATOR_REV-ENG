package com.plaud.template.debug

import android.app.Activity
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import com.tinnotech.penblesdk.entity.BleDevice
import sdk.PlaudDeviceAgent
import sdk.PlaudDeviceAgentListener
import sdk.audio.AudioExportFormat
import sdk.audio.AudioExporter
import sdk.audio.ExportStage
import sdk.ble.wifi.IWifiTransferAgent
import java.io.File
import java.security.MessageDigest

/**
 * R7-S14 Wi-Fi fast-transfer runtime driver (DEBUG-ONLY, not part of the product UI).
 *
 * Purpose: drive the GENUINE official SDK's Wi-Fi transfer path against our own
 * emulated pen and record exactly how far it gets. Public SDK API only; the
 * BLE bind is the R7-S13 one (recoveryConnectBleDevice with a SYNTHETIC
 * historical id, emulator at portVersion 7, no cloud material involved).
 *
 *   mode=transfer (default)  the template's SyncManager.startWiFiTransfer order:
 *                            stopSyncFile() -> 1.5 s grace -> startWifiTransfer(arg, cb).
 *                            READY / onHandshakeCompleted -> getWifiAgent().getFileList();
 *                            onFileListReceived -> exportAudioViaWiFi(sid, dir, OPUS, 1, cb),
 *                            output hashed. onError -> guard probes (getFileList,
 *                            exportAudioViaWiFi while not READY), then the template's
 *                            failure teardown: endWiFiTransfer() + setDeviceWiFi(false).
 *   mode=open                setDeviceWiFi(true), hold `hold` seconds, setDeviceWiFi(false);
 *                            logs the bleWiFiOpen callbacks (the facade reports both
 *                            the open and the close result through it).
 *
 *   adb shell am start -n com.plaud.template/.debug.WifiCaptureActivity \
 *       --es id "SYNTH-HIST-0001" --es mode transfer --es arg "SYNTH-WIFI-USER-0001"
 *
 * Every event is mirrored to logcat with a WIFICAP_ prefix; export output goes
 * to filesDir/wificap-export/ so `adb shell run-as` can pull it.
 *
 * Nothing here joins, fakes or approves a Wi-Fi network: whatever the SDK
 * asks Android for (WifiNetworkSpecifier / requestNetwork) is left to the OS.
 */
class WifiCaptureActivity : Activity() {

    private val tag = "WIFICAP"
    private val main = Handler(Looper.getMainLooper())
    @Volatile private var fired = false
    @Volatile private var bound = false
    @Volatile private var fileListRequested = false
    @Volatile private var exported = false
    @Volatile private var finished = false
    @Volatile private var mode = "transfer"
    @Volatile private var arg = "SYNTH-WIFI-USER-0001"
    @Volatile private var holdS = 8
    @Volatile private var startedAt = 0L

    private fun now() = System.currentTimeMillis()
    private fun rel() = if (startedAt == 0L) -1L else now() - startedAt

    private fun sha256(bytes: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }

    private fun hashFile(path: String?): String {
        if (path == null) return "path=null"
        return try {
            val f = File(path)
            if (!f.exists()) "path=$path exists=false"
            else "path=$path len=${f.length()} sha256=${sha256(f.readBytes())}"
        } catch (e: Exception) {
            "path=$path unreadable=${e.javaClass.simpleName}"
        }
    }

    private fun agentState(): String = try {
        val a = PlaudDeviceAgent.getWifiAgent()
        "agent=${a != null} state=${a?.getConnectionState()} active=${PlaudDeviceAgent.isWifiTransferActive()}"
    } catch (e: Exception) {
        "state_threw=${e.javaClass.simpleName}"
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val id = intent.getStringExtra("id") ?: "SYNTH-HIST-0001"
        mode = intent.getStringExtra("mode") ?: "transfer"
        arg = intent.getStringExtra("arg") ?: "SYNTH-WIFI-USER-0001"
        holdS = intent.getIntExtra("hold", 8)
        Log.i(tag, "WIFICAP_START id=\"$id\" mode=$mode arg=\"$arg\" hold=$holdS")

        PlaudDeviceAgent.listener = object : PlaudDeviceAgentListener {
            override fun bleScanResult(devices: List<BleDevice>) {
                if (fired) return
                val dev = devices.firstOrNull() ?: return
                fired = true
                Log.i(tag, "WIFICAP_SCAN sn=${dev.getSerialNumber()} projectCode=${dev.getProjectCode()} " +
                    "pv=${dev.getPortVersion()} wifiName=${dev.getWiFiName()}")
                try { PlaudDeviceAgent.stopScan() } catch (e: Exception) { Log.w(tag, "stopScan threw", e) }
                try {
                    PlaudDeviceAgent.recoveryConnectBleDevice(dev, id)
                } catch (e: Exception) {
                    Log.e(tag, "WIFICAP_RECOVERY_THREW", e)
                }
            }

            override fun bleConnectState(state: Int) {
                Log.i(tag, "WIFICAP_CONNECT_STATE state=$state t=${now()}")
            }

            override fun bleConnectStage(sn: String?, stage: String, detail: String?) {
                Log.i(tag, "WIFICAP_STAGE stage=\"$stage\" detail=\"$detail\"")
            }

            override fun bleBind(sn: String?, status: Int, protVersion: Int, timezone: Int) {
                Log.i(tag, "WIFICAP_BIND sn=\"$sn\" status=$status protVersion=$protVersion tz=$timezone")
                if (status != 0 || bound) return
                bound = true
                when (mode) {
                    "open" -> main.postDelayed({ runOpen() }, 1000)
                    else -> main.postDelayed({ runTransfer() }, 1000)
                }
            }

            override fun bleWiFiOpen(status: Int, wifiName: String, wholeName: String, wifiPass: String) {
                // Field names are the facade's; the SDK fills the three strings with "".
                Log.i(tag, "WIFICAP_BLE_WIFI_OPEN status=$status wifiName=\"$wifiName\" wholeName=\"$wholeName\" " +
                    "passLen=${wifiPass.length} t=${now()} rel=${rel()}")
            }
        }

        // BLANK ON PURPOSE (R7-S14 D7). PlaudDeviceAgent.initSDK -> NiceBuildSdk.initSdk
        // stores a non-blank token and then immediately POSTs partner/sdk/gen-key to
        // Plaud's server ("Partner API: Token 可用，正在获取 RSA 密钥对...",
        // build/evidence/javap/sdk/NiceBuildSdk.txt initSdk, isBlank branch). Earlier
        // runs passed a synthetic JWT here and so sent that request once per app start;
        // of 22 logged, 19 were answered 401 and 3 failed at DNS. A blank token takes the clearPartnerData()
        // branch instead, and nothing on the legacy recovery path needs a token.
        val initToken = ""
        try {
            PlaudDeviceAgent.initSDK(applicationContext, initToken, "api.plaud.ai")
            PlaudDeviceAgent.setBleLogLevel(Log.INFO)
        } catch (e: Exception) {
            Log.e(tag, "WIFICAP_INIT_THREW", e)
        }
        try {
            PlaudDeviceAgent.startScan()
            Log.i(tag, "WIFICAP_SCAN_STARTED")
        } catch (e: Exception) {
            Log.e(tag, "WIFICAP_SCAN_THREW", e)
        }
    }

    // --- mode=open: the public setDeviceWiFi(Boolean) pair ---------------------------

    private fun runOpen() {
        startedAt = now()
        Log.i(tag, "WIFICAP_SET_DEVICE_WIFI on=true t=${now()}")
        try { PlaudDeviceAgent.setDeviceWiFi(true) } catch (e: Exception) { Log.e(tag, "WIFICAP_SET_DEVICE_WIFI_THREW", e) }
        main.postDelayed({
            Log.i(tag, "WIFICAP_PROBE ${agentState()} rel=${rel()}")
            Log.i(tag, "WIFICAP_SET_DEVICE_WIFI on=false t=${now()} rel=${rel()}")
            try { PlaudDeviceAgent.setDeviceWiFi(false) } catch (e: Exception) { Log.e(tag, "WIFICAP_SET_DEVICE_WIFI_THREW", e) }
        }, holdS * 1000L)
        main.postDelayed({ Log.i(tag, "WIFICAP_END mode=open rel=${rel()}") }, holdS * 1000L + 12000L)
    }

    // --- mode=transfer: SyncManager.startWiFiTransfer / beginWiFiSession --------------

    private fun runTransfer() {
        Log.i(tag, "WIFICAP_PRE ${agentState()}")
        try {
            PlaudDeviceAgent.stopSyncFile()   // the template always stops a BLE transfer first
            Log.i(tag, "WIFICAP_STOPSYNC_CALLED t=${now()}")
        } catch (e: Exception) {
            Log.w(tag, "WIFICAP_STOPSYNC_THREW", e)
        }
        main.postDelayed({ beginWifi() }, 1500)   // SyncManager.DEVICE_IDLE_GRACE_MS
    }

    private fun beginWifi() {
        startedAt = now()
        Log.i(tag, "WIFICAP_START_WIFI_TRANSFER arg=\"$arg\" t=$startedAt")
        val started = try {
            PlaudDeviceAgent.startWifiTransfer(arg, callback)
        } catch (e: Exception) {
            Log.e(tag, "WIFICAP_START_WIFI_TRANSFER_THREW", e); false
        }
        Log.i(tag, "WIFICAP_START_WIFI_TRANSFER_RETURNED started=$started rel=${rel()}")
        // State probes while the SDK works (the join has a 30 s request timeout).
        for (s in listOf(2, 5, 10, 15, 20, 25, 29, 33, 40)) {
            main.postDelayed({ if (!finished) Log.i(tag, "WIFICAP_PROBE s=$s ${agentState()} rel=${rel()}") }, s * 1000L)
        }
    }

    private fun requestFileListOnce(why: String) {
        if (fileListRequested) return
        fileListRequested = true
        val ok = try { PlaudDeviceAgent.getWifiAgent()?.getFileList() } catch (e: Exception) { Log.e(tag, "WIFICAP_GETFILELIST_THREW", e); null }
        Log.i(tag, "WIFICAP_GETFILELIST why=$why returned=$ok rel=${rel()}")
    }

    private val callback = object : IWifiTransferAgent.WifiTransferCallback {
        override fun onConnectionStateChanged(state: IWifiTransferAgent.WifiConnectionState) {
            Log.i(tag, "WIFICAP_CB_STATE state=$state rel=${rel()}")
            if (state == IWifiTransferAgent.WifiConnectionState.READY) requestFileListOnce("READY")
        }

        override fun onHandshakeCompleted(info: String) {
            Log.i(tag, "WIFICAP_CB_HANDSHAKE_COMPLETED info=\"$info\" rel=${rel()}")
            requestFileListOnce("handshake")
        }

        override fun onFileListReceived(files: List<IWifiTransferAgent.WifiFileInfo>) {
            Log.i(tag, "WIFICAP_CB_FILELIST n=${files.size} rel=${rel()}")
            for (f in files) {
                Log.i(tag, "WIFICAP_FILE sessionId=${f.sessionId} name=${f.fileName} size=${f.fileSize} " +
                    "duration=${f.duration} timestamp=${f.timestamp} scene=${f.scene}")
            }
            val first = files.firstOrNull() ?: return
            if (!exported) { exported = true; export(first.sessionId, "list") }
        }

        override fun onTransferProgress(sessionId: Long, progress: Int, speed: Double) {
            if (progress % 25 == 0 || progress >= 99) Log.i(tag, "WIFICAP_CB_PROGRESS sessionId=$sessionId p=$progress speed=$speed rel=${rel()}")
        }

        override fun onFileTransferCompleted(sessionId: Long, path: String) {
            Log.i(tag, "WIFICAP_CB_FILE_DONE sessionId=$sessionId ${hashFile(path)} rel=${rel()}")
        }

        override fun onBatchDownloadStarted(total: Int) { Log.i(tag, "WIFICAP_CB_BATCH_STARTED total=$total") }
        override fun onBatchDownloadProgress(current: Int, total: Int, filename: String) {
            Log.i(tag, "WIFICAP_CB_BATCH_PROGRESS $current/$total $filename")
        }
        override fun onBatchDownloadCompleted(success: Int, failed: Int, results: List<IWifiTransferAgent.BatchDownloadResult>) {
            Log.i(tag, "WIFICAP_CB_BATCH_DONE success=$success failed=$failed")
        }
        override fun onFileDeleteCompleted(success: Boolean, deletedCount: Int, error: String?) {
            Log.i(tag, "WIFICAP_CB_DELETE success=$success n=$deletedCount error=$error")
        }

        override fun onWifiTransferStopped() {
            Log.i(tag, "WIFICAP_CB_STOPPED rel=${rel()}")
        }

        override fun onDeviceBatteryUpdate(level: Int, charging: Boolean) {
            Log.i(tag, "WIFICAP_CB_BATTERY level=$level charging=$charging rel=${rel()}")
        }

        override fun onError(code: Int, message: String) {
            Log.e(tag, "WIFICAP_CB_ERROR code=$code message=\"$message\" rel=${rel()} ${agentState()}")
            if (finished) return
            finished = true
            main.post { afterError() }
        }
    }

    /** Guard probes on a session that never reached READY, then the template's failure teardown. */
    private fun afterError() {
        val listOk = try { PlaudDeviceAgent.getWifiAgent()?.getFileList() } catch (e: Exception) { null }
        Log.i(tag, "WIFICAP_GUARD getFileList returned=$listOk ${agentState()}")
        export(1700000000L, "guard")
        main.postDelayed({
            Log.i(tag, "WIFICAP_END_WIFI_TRANSFER rel=${rel()}")
            try { PlaudDeviceAgent.endWiFiTransfer() } catch (e: Exception) { Log.e(tag, "WIFICAP_END_THREW", e) }
            val bleUp = try { PlaudDeviceAgent.isConnected() } catch (e: Exception) { false }
            Log.i(tag, "WIFICAP_TEARDOWN bleUp=$bleUp -> setDeviceWiFi(false) rel=${rel()}")
            if (bleUp) {
                try { PlaudDeviceAgent.setDeviceWiFi(false) } catch (e: Exception) { Log.e(tag, "WIFICAP_SET_DEVICE_WIFI_THREW", e) }
            }
        }, 1000)
        main.postDelayed({ Log.i(tag, "WIFICAP_END mode=transfer ${agentState()} rel=${rel()}") }, 12000)
    }

    private fun export(sessionId: Long, why: String) {
        val dir = File(filesDir, "wificap-export").apply { mkdirs() }
        Log.i(tag, "WIFICAP_EXPORT why=$why sessionId=$sessionId format=OPUS channels=1 rel=${rel()}")
        try {
            PlaudDeviceAgent.exportAudioViaWiFi(sessionId, dir, AudioExportFormat.OPUS, 1, object : AudioExporter.ExportCallback {
                override fun onProgress(progress: Int, message: String) {
                    if (progress % 25 == 0 || progress >= 99) Log.i(tag, "WIFICAP_EXPORT_PROGRESS why=$why p=$progress msg=\"$message\" rel=${rel()}")
                }
                override fun onStageChanged(stage: ExportStage) {
                    Log.i(tag, "WIFICAP_EXPORT_STAGE why=$why stage=$stage rel=${rel()}")
                }
                override fun onComplete(outputFile: File) {
                    Log.i(tag, "WIFICAP_EXPORT_DONE why=$why ${hashFile(outputFile.absolutePath)} rel=${rel()}")
                }
                override fun onError(error: String) {
                    Log.e(tag, "WIFICAP_EXPORT_ERROR why=$why \"$error\" rel=${rel()}")
                }
            })
        } catch (e: Exception) {
            Log.e(tag, "WIFICAP_EXPORT_THREW why=$why", e)
        }
    }
}
