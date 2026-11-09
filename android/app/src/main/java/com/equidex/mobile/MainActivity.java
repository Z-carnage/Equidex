package com.equidex.mobile;
import android.app.Activity;
import android.os.Build;
import android.graphics.Color;
import android.view.View;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.os.Bundle;
import android.os.Message;
import android.view.WindowInsets;
import android.content.Intent;
import android.net.Uri;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.widget.TextView;
import android.widget.FrameLayout;
import com.chaquo.python.Python;

public class MainActivity extends Activity {
    private WebView web;
    private ConnectivityManager connectivity;
    private ConnectivityManager.NetworkCallback connectivityCallback;
    private boolean internetAvailable() {
        if (connectivity == null) return true; // Unknown is not proof of disconnection.
        try {
            NetworkCapabilities caps = connectivity.getNetworkCapabilities(connectivity.getActiveNetwork());
            return caps != null && caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                && caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED);
        } catch (SecurityException ignored) { return true; }
    }
    private void publishNetworkState() {
        if (web != null && !isFinishing() && !isDestroyed())
            web.evaluateJavascript("window.__eqNetworkChanged && window.__eqNetworkChanged(" + internetAvailable() + ")", null);
    }
    private int insetTop, insetRight, insetBottom, insetLeft;
    private boolean keyboard;
    private int keyboardInset;
    private void presentation() {
        if (web == null) return;
        float density = getResources().getDisplayMetrics().density;
        web.evaluateJavascript("window.__eqDevicePresentation && window.__eqDevicePresentation(" +
            insetTop/density + "," + insetRight/density + "," + insetBottom/density + "," + insetLeft/density + "," +
            getResources().getConfiguration().fontScale + "," + keyboard + "," + keyboardInset/density + ")", null);
    }
    private void appearance(boolean light) {
        int flags = View.SYSTEM_UI_FLAG_LAYOUT_STABLE | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION;
        if (light) flags |= View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR;
        if (light && Build.VERSION.SDK_INT >= 26) flags |= View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR;
        getWindow().getDecorView().setSystemUiVisibility(flags);
        getWindow().setStatusBarColor(Color.TRANSPARENT);
        getWindow().setNavigationBarColor(Color.TRANSPARENT);
        if (Build.VERSION.SDK_INT >= 29) getWindow().setNavigationBarContrastEnforced(false);
    }
    public class PresentationBridge {
        @JavascriptInterface public void appearance(boolean light) { runOnUiThread(() -> MainActivity.this.appearance(light)); }
        @JavascriptInterface public boolean isInternetAvailable() { return MainActivity.this.internetAvailable(); }
    }
    private boolean local(Uri uri) { return "127.0.0.1".equals(uri.getHost()) && "http".equals(uri.getScheme()); }
    private void external(Uri uri) {
        if ("https".equals(uri.getScheme()) || "http".equals(uri.getScheme())) {
            try { startActivity(new Intent(Intent.ACTION_VIEW, uri)); } catch (Exception ignored) { }
        }
    }
    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        connectivity = (ConnectivityManager)getSystemService(CONNECTIVITY_SERVICE);
        if (connectivity != null) {
            connectivityCallback = new ConnectivityManager.NetworkCallback() {
                @Override public void onAvailable(Network network) { runOnUiThread(() -> publishNetworkState()); }
                @Override public void onLost(Network network) { runOnUiThread(() -> publishNetworkState()); }
                @Override public void onCapabilitiesChanged(Network network, NetworkCapabilities caps) { runOnUiThread(() -> publishNetworkState()); }
            };
            try { connectivity.registerDefaultNetworkCallback(connectivityCallback); }
            catch (RuntimeException ignored) { connectivityCallback = null; }
        }
        if (Build.VERSION.SDK_INT >= 30) getWindow().setDecorFitsSystemWindows(false);
        appearance(false);
        getWindow().setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);
        if (Build.VERSION.SDK_INT >= 28) {
            WindowManager.LayoutParams attributes = getWindow().getAttributes();
            attributes.layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
            getWindow().setAttributes(attributes);
        }
        FrameLayout loading = new FrameLayout(this);
        loading.setBackgroundColor(Color.rgb(10, 10, 10));
        setContentView(loading);
        new Thread(() -> {
            try {
                String url = Python.getInstance().getModule("android_server").callAttr("start").toString();
                runOnUiThread(() -> {
                    if (isFinishing() || isDestroyed()) return;
                    web = new WebView(this);
                    web.setBackgroundColor(Color.rgb(10, 10, 10));
                    web.addJavascriptInterface(new PresentationBridge(), "EquidexPresentation");
                    WebSettings settings = web.getSettings();
                    settings.setTextZoom(100);
                    settings.setJavaScriptEnabled(true); settings.setDomStorageEnabled(true);
                    settings.setAllowFileAccess(false); settings.setAllowContentAccess(false);
                    settings.setSupportMultipleWindows(true);
                    settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
                    web.setWebViewClient(new WebViewClient() {
                        @Override public void onPageFinished(WebView view, String url) { presentation(); publishNetworkState(); view.evaluateJavascript("typeof syncDeviceTheme==='function' && syncDeviceTheme()", null); }
                        @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                            if (local(request.getUrl())) return false;
                            external(request.getUrl()); return true;
                        }
                    });
                    web.setWebChromeClient(new WebChromeClient() {
                        @Override public boolean onCreateWindow(WebView view, boolean dialog, boolean gesture, Message result) {
                            if (!gesture) return false;
                            WebView popup = new WebView(MainActivity.this);
                            popup.setWebViewClient(new WebViewClient() {
                                @Override public boolean shouldOverrideUrlLoading(WebView v, String url) { external(Uri.parse(url)); v.destroy(); return true; }
                                @Override public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) { external(r.getUrl()); v.destroy(); return true; }
                            });
                            ((WebView.WebViewTransport)result.obj).setWebView(popup); result.sendToTarget(); return true;
                        }
                    });
                    web.setOnApplyWindowInsetsListener((v, insets) -> {
                        if (Build.VERSION.SDK_INT >= 30) {
                            android.graphics.Insets bars = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
                            insetTop=bars.top; insetRight=bars.right; insetBottom=bars.bottom; insetLeft=bars.left;
                            keyboard=insets.isVisible(WindowInsets.Type.ime());
                            keyboardInset=keyboard ? insets.getInsets(WindowInsets.Type.ime()).bottom : 0;
                        } else {
                            insetTop=insets.getSystemWindowInsetTop(); insetRight=insets.getSystemWindowInsetRight();
                            insetLeft=insets.getSystemWindowInsetLeft();
                            int bottom=insets.getSystemWindowInsetBottom();
                            keyboard=bottom > 100 * getResources().getDisplayMetrics().density;
                            if (!keyboard) insetBottom=bottom;
                        }
                        presentation(); return insets;
                    });
                    setContentView(web);
                    web.requestApplyInsets();
                    if (state == null || web.restoreState(state) == null) web.loadUrl(url);
                });
            } catch (Exception e) { runOnUiThread(() -> {
                if (isFinishing() || isDestroyed()) return;
                TextView error = new TextView(this);
                error.setText("Unable to start EQUIDEX. Close and reopen the app.");
                error.setTextColor(Color.WHITE);
                loading.addView(error);
            }); }
        }).start();
    }
    @Override public void onBackPressed() {
        if (web == null) { super.onBackPressed(); return; }
        web.evaluateJavascript("(function(){if(typeof drawerOpen==='function' && drawerOpen()){setDrawer(false);return true;}return false;})()", result -> {
            if (!"true".equals(result)) { if(web.canGoBack()) web.goBack(); else MainActivity.super.onBackPressed(); }
        });
    }
    @Override protected void onSaveInstanceState(Bundle state) { if(web != null) web.saveState(state); super.onSaveInstanceState(state); }
    @Override protected void onPause() {
        if (web != null) {
            web.evaluateJavascript("window.__eqHostVisibility && window.__eqHostVisibility(false)", null);
            web.onPause();
        }
        super.onPause();
    }
    @Override protected void onResume() {
        super.onResume();
        if (web != null) {
            web.onResume();
            publishNetworkState();
            web.evaluateJavascript("window.__eqHostVisibility && window.__eqHostVisibility(true)", null);
        }
    }
    @Override protected void onDestroy() {
        if(connectivity != null && connectivityCallback != null) {
            try { connectivity.unregisterNetworkCallback(connectivityCallback); } catch (RuntimeException ignored) { }
        }
        if(web != null) { web.stopLoading(); web.destroy(); }
        super.onDestroy();
    }
}
