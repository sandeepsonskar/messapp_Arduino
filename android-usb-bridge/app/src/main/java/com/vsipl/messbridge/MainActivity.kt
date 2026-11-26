package com.vsipl.messbridge

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.Manifest
import android.content.pm.PackageManager
import android.webkit.PermissionRequest
import android.content.Intent
import android.content.IntentFilter
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbManager
import android.os.Bundle
import android.webkit.JavascriptInterface
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.hoho.android.usbserial.driver.UsbSerialPort
import com.hoho.android.usbserial.driver.UsbSerialProber

class MainActivity : AppCompatActivity() {
    companion object {
        // Change this to the HTTPS supervisor address printed by run_local.py.
        private const val START_URL = "https://100.71.122.1:5443/scan"
        private const val ACTION_USB_PERMISSION = "com.vsipl.messbridge.USB_PERMISSION"
    }

    private lateinit var web: WebView
    private lateinit var status: TextView
    private val usbManager by lazy { getSystemService(Context.USB_SERVICE) as UsbManager }
    private var port: UsbSerialPort? = null

    private val permissionReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            if (intent.action == ACTION_USB_PERMISSION) {
                val device = intent.getParcelableExtra<UsbDevice>(UsbManager.EXTRA_DEVICE)
                if (intent.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED, false) && device != null) {
                    openArduino(device)
                } else setStatus("USB permission denied. Reconnect Arduino and allow access.")
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val layout = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        status = TextView(this).apply { text = "Connect Arduino Uno using USB OTG"; setPadding(16, 12, 16, 12) }
        web = WebView(this)
        layout.addView(status)
        layout.addView(web, LinearLayout.LayoutParams(-1, 0, 1f))
        setContentView(layout)

        registerReceiver(permissionReceiver, IntentFilter(ACTION_USB_PERMISSION))
        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true
        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.CAMERA), 41)
        }
        web.webChromeClient = object : WebChromeClient() {
            override fun onPermissionRequest(request: PermissionRequest) {
                runOnUiThread {
                    if (checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
                        request.grant(request.resources)
                    } else {
                        setStatus("Allow camera permission, then reload the scanner")
                    }
                }
            }
        }
        web.addJavascriptInterface(ArduinoBridge(), "AndroidBridge")
        web.webViewClient = object : WebViewClient() {
            override fun onPageFinished(view: WebView, url: String) {
                super.onPageFinished(view, url)
                if (url.startsWith(START_URL.substringBefore("/scan"))) setStatus("Website loaded. Checking USB Arduino…")
            }
        }
        web.loadUrl(START_URL)
        detectArduino()
    }

    private fun detectArduino() {
        val drivers = UsbSerialProber.getDefaultProber().findAllDrivers(usbManager)
        if (drivers.isEmpty()) { setStatus("Arduino not detected. Check USB OTG and cable."); return }
        val device = drivers[0].device
        if (!usbManager.hasPermission(device)) {
            val pi = PendingIntent.getBroadcast(this, 0, Intent(ACTION_USB_PERMISSION), PendingIntent.FLAG_MUTABLE)
            usbManager.requestPermission(device, pi)
        } else openArduino(device)
    }

    @Suppress("DEPRECATION")
    private fun openArduino(device: UsbDevice) {
        try {
            val driver = UsbSerialProber.getDefaultProber().probeDevice(device)
                ?: throw IllegalStateException("Unsupported USB serial device")
            val connection = usbManager.openDevice(device) ?: throw IllegalStateException("Cannot open USB device")
            val p = driver.ports.first()
            p.open(connection)
            p.setParameters(9600, 8, UsbSerialPort.STOPBITS_1, UsbSerialPort.PARITY_NONE)
            port = p
            setStatus("Arduino connected over USB at 9600 baud")
        } catch (e: Exception) {
            setStatus("Arduino connection error: ${e.message}")
        }
    }

    inner class ArduinoBridge {
        @JavascriptInterface
        fun sendUnlock() {
            runOnUiThread {
                try {
                    val p = port ?: run { setStatus("Unlock not sent: Arduino is not connected"); return@runOnUiThread }
                    p.write("ACCESS_GRANTED\n".toByteArray(Charsets.US_ASCII), 1000)
                    setStatus("ACCESS GRANTED — unlock command sent")
                    Toast.makeText(this@MainActivity, "Access granted", Toast.LENGTH_SHORT).show()
                } catch (e: Exception) {
                    setStatus("USB write failed: ${e.message}")
                }
            }
        }
    }

    private fun setStatus(s: String) { runOnUiThread { status.text = s } }

    override fun onDestroy() {
        try { unregisterReceiver(permissionReceiver) } catch (_: Exception) {}
        try { port?.close() } catch (_: Exception) {}
        web.destroy()
        super.onDestroy()
    }
}
