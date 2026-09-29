package com.plaud.template.debug

import android.app.Activity
import android.os.Bundle
import android.util.Log
import com.tinnotech.penblesdk.entity.BleDevice
import sdk.PlaudDeviceAgent
import sdk.PlaudDeviceAgentListener

/**
 * R7-S12 k3 runtime capture driver (DEBUG-ONLY, not part of the product UI).
 *
 * Purpose: make the GENUINE official SDK construct and write its k3
 * (first_handshake) frame to 1910/2BB1, using ONLY public SDK entry points and
 * a SYNTHETIC historical identifier, against our own Bumble emulator over
 * netsim. No credential, no cloud, no authentication is involved:
 *   - recoveryConnectBleDevice(device, id) derives the k3 token by a pure local
 *     string transform of `id` (strip "client_user_", drop '-'); it makes no
 *     network call and reuses no cloud material on the legacy (pv<20) path.
 *   - The emulator advertises portVersion 7, so the SDK goes straight to
 *     first_handshake; k3 is written iff the token is non-empty.
 *
 * Drive it with, e.g.:
 *   adb shell am start -n com.plaud.template/.debug.K3CaptureActivity \
 *       --es id "SYNTH-HIST-0001"
 * The k3 bytes are captured on the PERIPHERAL side (r7/k3_capture_peripheral.py);
 * this Activity only triggers the SDK and mirrors stage callbacks to logcat.
 */
class K3CaptureActivity : Activity() {

    private val tag = "K3CAP"
    @Volatile private var fired = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val id = intent.getStringExtra("id") ?: "SYNTH-HIST-0001"
        val targetSn = intent.getStringExtra("sn") // optional filter; else first device
        Log.i(tag, "K3CAP_START id=\"$id\" targetSn=${targetSn ?: "<first>"}")

        PlaudDeviceAgent.listener = object : PlaudDeviceAgentListener {
            override fun bleScanResult(devices: List<BleDevice>) {
                if (fired) return
                Log.i(tag, "K3CAP_SCAN n=${devices.size} sns=${devices.map { it.getSerialNumber() }}")
                val dev = if (targetSn != null)
                    devices.firstOrNull { it.getSerialNumber() == targetSn }
                else devices.firstOrNull()
                if (dev == null) return
                fired = true
                try {
                    PlaudDeviceAgent.stopScan()
                } catch (e: Exception) { Log.w(tag, "stopScan threw", e) }
                Log.i(tag, "K3CAP_RECOVERY calling recoveryConnectBleDevice sn=${dev.getSerialNumber()} id=\"$id\"")
                try {
                    PlaudDeviceAgent.recoveryConnectBleDevice(dev, id)
                } catch (e: Exception) {
                    Log.e(tag, "K3CAP_RECOVERY_THREW", e)
                }
            }

            override fun bleConnectState(state: Int) {
                Log.i(tag, "K3CAP_CONNECT_STATE state=$state")
            }

            override fun bleConnectStage(sn: String?, stage: String, detail: String?) {
                Log.i(tag, "K3CAP_STAGE sn=\"$sn\" stage=\"$stage\" detail=\"$detail\"")
            }

            override fun bleBind(sn: String?, status: Int, protVersion: Int, timezone: Int) {
                Log.i(tag, "K3CAP_BIND sn=\"$sn\" status=$status protVersion=$protVersion tz=$timezone")
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
            Log.e(tag, "K3CAP_INIT_THREW", e)
        }

        try {
            PlaudDeviceAgent.startScan()
            Log.i(tag, "K3CAP_SCAN_STARTED")
        } catch (e: Exception) {
            Log.e(tag, "K3CAP_SCAN_THREW", e)
        }
    }
}
