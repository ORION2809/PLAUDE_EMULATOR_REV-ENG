package com.example.pingpong

import android.Manifest
import android.annotation.SuppressLint
import android.bluetooth.*
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.app.Activity
import java.util.UUID

class MainActivity : Activity() {
    companion object {
        const val TAG = "PingPong"
        // Generic 128-bit UUIDs; exact values do not matter for transport.
        val SERVICE_UUID: UUID = UUID.fromString("0000feed-0000-1000-8000-00805f9b34fb")
        val WRITE_UUID: UUID = UUID.fromString("0000fe01-0000-1000-8000-00805f9b34fb")
        val NOTIFY_UUID: UUID = UUID.fromString("0000fe02-0000-1000-8000-00805f9b34fb")
        val CCCD_UUID: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
    }

    private val handler = Handler(Looper.getMainLooper())
    private var gatt: BluetoothGatt? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Log.i(TAG, "PINGPONG_APP_START")
        ensurePermissionsThen { startScan() }
    }

    private fun ensurePermissionsThen(next: () -> Unit) {
        val need = mutableListOf<String>()
        if (Build.VERSION.SDK_INT >= 31) {
            need += Manifest.permission.BLUETOOTH_SCAN
            need += Manifest.permission.BLUETOOTH_CONNECT
        } else {
            need += Manifest.permission.ACCESS_FINE_LOCATION
        }
        val missing = need.filter {
            checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing.isEmpty()) next() else requestPermissions(missing.toTypedArray(), 1)
    }

    override fun onRequestPermissionsResult(code: Int, perms: Array<String>, res: IntArray) {
        super.onRequestPermissionsResult(code, perms, res)
        if (res.all { it == PackageManager.PERMISSION_GRANTED }) startScan()
        else Log.e(TAG, "PINGPONG_PERMISSIONS_DENIED")
    }

    @SuppressLint("MissingPermission")
    private fun startScan() {
        val adapter = BluetoothAdapter.getDefaultAdapter()
        Log.i(TAG, "PINGPONG_ADAPTER enabled=${adapter?.isEnabled} le=${packageManager.hasSystemFeature(PackageManager.FEATURE_BLUETOOTH_LE)}")
        val scanner = adapter?.bluetoothLeScanner
        if (scanner == null) {
            Log.e(TAG, "PINGPONG_NO_SCANNER")
            return
        }
        Log.i(TAG, "PINGPONG_SCAN_START")
        scanner.startScan(object : ScanCallback() {
            override fun onScanResult(type: Int, result: ScanResult) {
                val uuids = result.scanRecord?.serviceUuids?.map { it.uuid } ?: emptyList()
                Log.i(TAG, "PINGPONG_SCAN_SEEN addr=${result.device.address} uuids=$uuids")
                if (SERVICE_UUID in uuids) {
                    Log.i(TAG, "PINGPONG_SCAN_MATCH")
                    scanner.stopScan(this)
                    connect(result.device)
                }
            }

            override fun onScanFailed(code: Int) {
                Log.e(TAG, "PINGPONG_SCAN_FAILED code=$code")
            }
        })
        handler.postDelayed({ Log.e(TAG, "PINGPONG_SCAN_TIMEOUT") }, 60_000)
    }

    @SuppressLint("MissingPermission")
    private fun connect(device: BluetoothDevice) {
        Log.i(TAG, "PINGPONG_CONNECT")
        gatt = device.connectGatt(this, false, callbacks)
    }

    private val callbacks = object : BluetoothGattCallback() {
        @SuppressLint("MissingPermission")
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            Log.i(TAG, "PINGPONG_CONN status=$status state=$newState")
            if (newState == BluetoothProfile.STATE_CONNECTED) g.discoverServices()
            else Log.i(TAG, "PINGPONG_DISCONNECTED")
        }

        @SuppressLint("MissingPermission")
        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            Log.i(TAG, "PINGPONG_SERVICES status=$status")
            val svc = g.getService(SERVICE_UUID)
            Log.i(TAG, "PINGPONG_SERVICE found=${svc != null}")
            val ntf = svc?.getCharacteristic(NOTIFY_UUID)
            if (ntf != null) {
                g.setCharacteristicNotification(ntf, true)
                ntf.getDescriptor(CCCD_UUID)?.let {
                    it.value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
                    g.writeDescriptor(it)
                }
            } else {
                val w = svc?.getCharacteristic(WRITE_UUID)
                if (w != null) writePing(g, w)
            }
        }

        override fun onDescriptorWrite(g: BluetoothGatt, d: BluetoothGattDescriptor, status: Int) {
            Log.i(TAG, "PINGPONG_CCCD status=$status")
            val w = g.getService(SERVICE_UUID)?.getCharacteristic(WRITE_UUID)
            if (w != null) writePing(g, w)
        }

        @SuppressLint("MissingPermission")
        private fun writePing(g: BluetoothGatt, w: BluetoothGattCharacteristic) {
            w.value = "ping".toByteArray()
            w.writeType = BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
            val ok = g.writeCharacteristic(w)
            Log.i(TAG, "PINGPONG_WRITE_PING queued=$ok")
        }

        override fun onCharacteristicWrite(g: BluetoothGatt, c: BluetoothGattCharacteristic, status: Int) {
            Log.i(TAG, "PINGPONG_WRITE_DONE status=$status")
        }

        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic) {
            val s = c.value?.toString(Charsets.UTF_8)
            Log.i(TAG, "PINGPONG_NOTIFIED value=$s")
            if (s == "pong") {
                Log.i(TAG, "PINGPONG_SUCCESS")
                g.disconnect()
            }
        }
    }
}
