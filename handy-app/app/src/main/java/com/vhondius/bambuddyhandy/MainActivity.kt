package com.vhondius.bambuddyhandy

import android.animation.ValueAnimator
import android.annotation.SuppressLint
import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.res.Configuration
import android.graphics.Bitmap
import android.net.Uri
import android.os.Bundle
import android.view.MotionEvent
import android.view.View
import android.webkit.CookieManager
import android.webkit.JavascriptInterface
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.app.AppCompatDelegate
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.interpolator.view.animation.FastOutSlowInInterpolator
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout
import com.google.android.material.bottomnavigation.BottomNavigationView
import java.io.ByteArrayInputStream
import java.net.URLEncoder

class MainActivity : AppCompatActivity() {

    private lateinit var makerworld: WebView
    private lateinit var bambuddy: WebView
    private lateinit var printer: WebView
    private lateinit var refreshMakerworld: SwipeRefreshLayout
    private lateinit var refreshBambuddy: SwipeRefreshLayout
    private lateinit var refreshPrinter: SwipeRefreshLayout
    private lateinit var nav: BottomNavigationView
    private lateinit var emptyBambuddy: View
    private lateinit var settingsTab: View
    /** Where Back and Save on the Settings tab return to. */
    private var previousTab = R.id.tab_makerworld
    private val injectJs by lazy { assets.open("inject.js").bufferedReader().use { it.readText() } }
    private val refreshGuardJs by lazy { assets.open("refresh-guard.js").bufferedReader().use { it.readText() } }

    /** Host allowed over http://, read from the request threads of both WebViews. */
    @Volatile private var bambuddyHost: String? = null
    private val guards = mutableListOf<RefreshGuard>()
    private var loadedBase: String? = null
    /** A print flow (or Bambuddy itself) is showing in the Bambuddy tab; otherwise it shows [emptyBambuddy]. */
    private var pageOpen = false
    /** Drop the Bambuddy tab's older pages once the page being opened has loaded. */
    private var clearBambuddyHistory = false
    /** A model link waiting for the Bambuddy address to be set. */
    private var pendingPrint: String? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        AppCompatDelegate.setDefaultNightMode(SettingsTab.nightMode(this))
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        // The bars sit on MakerWorld's header colour: dark icons on the light one.
        val light = resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK != Configuration.UI_MODE_NIGHT_YES
        WindowCompat.getInsetsController(window, window.decorView).apply {
            isAppearanceLightStatusBars = light
            isAppearanceLightNavigationBars = light
        }
        // Android 15+ draws edge to edge: keep the pages and the bottom nav
        // clear of the status bar, the gesture bar and the keyboard.
        ViewCompat.setOnApplyWindowInsetsListener(findViewById(R.id.root)) { v, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.ime())
            v.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            WindowInsetsCompat.CONSUMED
        }

        makerworld = findViewById(R.id.web_makerworld)
        bambuddy = findViewById(R.id.web_bambuddy)
        refreshMakerworld = findViewById(R.id.refresh_makerworld)
        refreshBambuddy = findViewById(R.id.refresh_bambuddy)
        printer = findViewById(R.id.web_printer)
        refreshPrinter = findViewById(R.id.refresh_printer)
        nav = findViewById(R.id.bottom_nav)
        emptyBambuddy = findViewById(R.id.empty_bambuddy)
        findViewById<View>(R.id.open_bambuddy).setOnClickListener {
            SettingsTab.baseUrl(this)?.let { openBambuddy(it) }
        }

        setUpWebView(makerworld, refreshMakerworld, injectButton = true)
        setUpWebView(bambuddy, refreshBambuddy, injectButton = false)
        setUpWebView(printer, refreshPrinter, injectButton = false)
        // Bambuddy leaves out its menu for this marker: the tab shows the
        // printers page and nothing else.
        printer.settings.userAgentString = printer.settings.userAgentString + " BambuddyHandyEmbedded"
        // Bambuddy takes MakerWorld's colours for this marker (contained in the one above).
        bambuddy.settings.userAgentString = bambuddy.settings.userAgentString + " BambuddyHandy"
        makerworld.addJavascriptInterface(PrintBridge(), "BambuddyHandy")
        bambuddy.addJavascriptInterface(AppBridge(), "BambuddyHandyApp")

        settingsTab = findViewById(R.id.settings_tab)
        SettingsTab(settingsTab) { base ->
            nav.selectedItemId = previousTab
            applyBase(base)
        }
        // No bar-wide tint: each icon colours itself by its checked state
        // (nav_icon in the vector paths; the Bambuddy logo turns full colour).
        nav.itemIconTintList = null
        nav.isItemActiveIndicatorEnabled = false
        nav.setOnItemSelectedListener { item ->
            if (nav.selectedItemId != R.id.tab_settings) previousTab = nav.selectedItemId
            showTab(item.itemId)
            if (ValueAnimator.areAnimatorsEnabled()) {
                nav.findViewById<View>(item.itemId)?.apply {
                    scaleX = 0.85f
                    scaleY = 0.85f
                    animate().scaleX(1f).scaleY(1f).setDuration(160).setInterpolator(EASE).start()
                }
            }
            true
        }
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (nav.selectedItemId == R.id.tab_settings) {
                    nav.selectedItemId = previousTab
                    return
                }
                val web = currentWebView()
                if (web !== bambuddy || !pageOpen) {
                    if (web === bambuddy) exit() else defaultBack(web)
                    return
                }
                // Bambuddy's print flow steps back through its own layers
                // (G-code preview, dialogs, settings) and then closes; only
                // when no flow is open does Back walk the page history.
                web.evaluateJavascript(
                    "(window.BambuddyHandyBack ? window.BambuddyHandyBack() : false)",
                ) { handled -> if (handled != "true") defaultBack(web) }
            }

            private fun defaultBack(web: WebView) {
                when {
                    web.canGoBack() -> web.goBack()
                    web === bambuddy -> closeBambuddy()
                    else -> exit()
                }
            }

            private fun exit() {
                isEnabled = false
                onBackPressedDispatcher.onBackPressed()
                isEnabled = true
            }
        })

        if (savedInstanceState != null) {
            makerworld.restoreState(savedInstanceState.getBundle(STATE_MAKERWORLD) ?: Bundle())
            bambuddy.restoreState(savedInstanceState.getBundle(STATE_BAMBUDDY) ?: Bundle())
            printer.restoreState(savedInstanceState.getBundle(STATE_PRINTER) ?: Bundle())
            loadedBase = savedInstanceState.getString(STATE_LOADED_BASE)
            pageOpen = savedInstanceState.getBoolean(STATE_FLOW_OPEN)
            showTab(nav.selectedItemId)
        } else {
            makerworld.loadUrl(MAKERWORLD_HOME)
        }
        val base = SettingsTab.baseUrl(this)
        if (base == null) {
            // First run: nothing to show in the Bambuddy tabs without an address.
            nav.selectedItemId = R.id.tab_settings
        } else {
            applyBase(base)
        }
        if (savedInstanceState == null) handleShare(intent)
    }

    /** Use [base] as the Bambuddy address, and open a print that was waiting for one. */
    private fun applyBase(base: String) {
        bambuddyHost = Uri.parse(base).host
        loadPrinter(base)
        pendingPrint?.let {
            pendingPrint = null
            openPrint(it)
        }
    }

    override fun dispatchTouchEvent(ev: MotionEvent): Boolean {
        // The refresh layout decides whether to track a pull at finger-down,
        // before refresh-guard.js can answer for this touch. Start from "yes"
        // so the pull is tracked; the script's answer arrives within a few ms
        // and the layout re-checks it on every move, well before the pull is
        // long enough to refresh.
        if (ev.actionMasked == MotionEvent.ACTION_DOWN) guards.forEach { it.allowed = true }
        return super.dispatchTouchEvent(ev)
    }

    override fun onPause() {
        super.onPause()
        CookieManager.getInstance().flush()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleShare(intent)
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putBundle(STATE_MAKERWORLD, Bundle().also { makerworld.saveState(it) })
        outState.putBundle(STATE_BAMBUDDY, Bundle().also { bambuddy.saveState(it) })
        outState.putBundle(STATE_PRINTER, Bundle().also { printer.saveState(it) })
        outState.putString(STATE_LOADED_BASE, loadedBase)
        outState.putBoolean(STATE_FLOW_OPEN, pageOpen)
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun setUpWebView(web: WebView, refresh: SwipeRefreshLayout, injectButton: Boolean) {
        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, true)
        refresh.setOnRefreshListener { web.reload() }
        refresh.setColorSchemeResources(R.color.mw_primary)
        refresh.setProgressBackgroundColorSchemeResource(R.color.mw_bg_top)
        val guard = RefreshGuard()
        guards += guard
        web.addJavascriptInterface(guard, "HandyRefresh")
        refresh.setOnChildScrollUpCallback { _, _ ->
            // Without the page script (an error page, say) fall back to the
            // WebView's own scroll position.
            if (guard.active) !guard.allowed else web.canScrollVertically(-1)
        }

        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val uri = request.url
                return when (uri.scheme) {
                    "https" -> false
                    "http" -> {
                        if (isAllowedCleartext(uri)) {
                            false
                        } else {
                            Toast.makeText(
                                this@MainActivity,
                                getString(R.string.blocked_cleartext, uri.host),
                                Toast.LENGTH_SHORT,
                            ).show()
                            true
                        }
                    }
                    // mailto:, intent:, app links: hand to the system, never load in place.
                    else -> {
                        try {
                            startActivity(Intent(Intent.ACTION_VIEW, uri))
                        } catch (_: ActivityNotFoundException) {
                        } catch (_: SecurityException) {
                        }
                        true
                    }
                }
            }

            override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse? {
                val uri = request.url
                if (uri.scheme == "http" && !isAllowedCleartext(uri)) {
                    return WebResourceResponse("text/plain", "utf-8", 403, "Blocked", null, ByteArrayInputStream(ByteArray(0)))
                }
                return null
            }

            override fun onPageStarted(view: WebView, url: String?, favicon: Bitmap?) {
                guard.active = false
                view.evaluateJavascript(refreshGuardJs, null)
                if (injectButton && isMakerworld(Uri.parse(url ?: ""))) view.evaluateJavascript(injectJs, null)
            }

            override fun onPageFinished(view: WebView, url: String?) {
                refresh.isRefreshing = false
                if (view === bambuddy && clearBambuddyHistory) {
                    clearBambuddyHistory = false
                    view.clearHistory()
                }
                view.evaluateJavascript(refreshGuardJs, null)
                if (injectButton && isMakerworld(Uri.parse(url ?: ""))) view.evaluateJavascript(injectJs, null)
            }
        }
    }

    /** Point the Printer tab at [base] when it is new or changed. */
    private fun loadPrinter(base: String) {
        if (base == loadedBase) return
        loadedBase = base
        printer.loadUrl(base)
    }

    private fun isAllowedCleartext(uri: Uri): Boolean {
        val host = bambuddyHost ?: return false
        return uri.host.equals(host, ignoreCase = true)
    }

    private fun currentWebView(): WebView = when (nav.selectedItemId) {
        R.id.tab_bambuddy -> bambuddy
        R.id.tab_printer -> printer
        else -> makerworld
    }

    private fun showTab(itemId: Int) {
        val target = when (itemId) {
            R.id.tab_makerworld -> refreshMakerworld
            R.id.tab_printer -> refreshPrinter
            R.id.tab_settings -> settingsTab
            else -> if (pageOpen) refreshBambuddy else emptyBambuddy
        }
        for (view in listOf(refreshMakerworld, refreshPrinter, settingsTab, refreshBambuddy, emptyBambuddy)) {
            if (view !== target) view.visibility = View.GONE
        }
        if (target.visibility == View.VISIBLE) return
        target.visibility = View.VISIBLE
        // A short fade-and-rise as the tab comes in; off with "Remove animations".
        if (ValueAnimator.areAnimatorsEnabled()) {
            target.alpha = 0f
            target.translationY = 12 * resources.displayMetrics.density
            target.animate().alpha(1f).translationY(0f).setDuration(180).setInterpolator(EASE).start()
        }
    }

    /** Open Bambuddy's one-tap print flow for a MakerWorld model link. */
    private fun openPrint(modelUrl: String) {
        val base = SettingsTab.baseUrl(this)
        if (base == null) {
            pendingPrint = modelUrl
            Toast.makeText(this, R.string.set_address_first, Toast.LENGTH_LONG).show()
            nav.selectedItemId = R.id.tab_settings
            return
        }
        loadPrinter(base)
        openBambuddy("$base/makerworld?url=" + URLEncoder.encode(modelUrl, "UTF-8"))
    }

    /** Show [url] in the Bambuddy tab with a fresh history, so Back ends at the empty screen. */
    private fun openBambuddy(url: String) {
        clearBambuddyHistory = true
        bambuddy.loadUrl(url)
        pageOpen = true
        if (nav.selectedItemId == R.id.tab_bambuddy) showTab(R.id.tab_bambuddy) else nav.selectedItemId = R.id.tab_bambuddy
    }

    private fun closeBambuddy() {
        pageOpen = false
        showTab(nav.selectedItemId)
    }

    private fun handleShare(intent: Intent?) {
        if (intent?.action != Intent.ACTION_SEND) return
        val text = intent.getStringExtra(Intent.EXTRA_TEXT) ?: return
        val link = MAKERWORLD_LINK.find(text)?.value
        if (link == null) {
            Toast.makeText(this, R.string.no_makerworld_link, Toast.LENGTH_LONG).show()
            return
        }
        openPrint(link)
    }

    /** Called by refresh-guard.js, from the WebView's JS thread. */
    private class RefreshGuard {
        @Volatile var active = false
        @Volatile var allowed = false

        @JavascriptInterface
        fun ready() {
            active = true
        }

        @JavascriptInterface
        fun report(value: Boolean) {
            allowed = value
        }
    }

    /** Called by Bambuddy's print flow. Only exposed to the Bambuddy tab. */
    inner class AppBridge {
        /** The flow was closed: go back to the MakerWorld tab it came from. */
        @JavascriptInterface
        fun closeFlow() {
            runOnUiThread {
                pageOpen = false
                nav.selectedItemId = R.id.tab_makerworld
            }
        }
    }

    /** Called by inject.js. Only exposed to the MakerWorld tab. */
    inner class PrintBridge {
        @JavascriptInterface
        fun print(url: String) {
            if (!isMakerworld(Uri.parse(url))) return
            runOnUiThread {
                // The bridge stays attached when the tab wanders off to another
                // site; only act while it is actually showing MakerWorld.
                if (isMakerworld(Uri.parse(makerworld.url ?: ""))) openPrint(url)
            }
        }
    }

    companion object {
        private const val MAKERWORLD_HOME = "https://makerworld.com/"
        private val EASE = FastOutSlowInInterpolator()
        private const val STATE_MAKERWORLD = "makerworld"
        private const val STATE_BAMBUDDY = "bambuddy"
        private const val STATE_PRINTER = "printer"
        private const val STATE_LOADED_BASE = "loaded_base"
        private const val STATE_FLOW_OPEN = "flow_open"
        private val MAKERWORLD_LINK = Regex("""https://(?:[\w-]+\.)*makerworld\.com/\S+""")

        fun isMakerworld(uri: Uri): Boolean {
            val host = uri.host?.lowercase() ?: return false
            return uri.scheme == "https" && (host == "makerworld.com" || host.endsWith(".makerworld.com"))
        }
    }
}
