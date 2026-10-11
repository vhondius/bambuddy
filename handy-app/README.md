# Bambuddy Handy (Android)

A small sideloaded app for printing MakerWorld models through your own
Bambuddy, running this fork (see the [main README](../README.md)).
It wears MakerWorld's look throughout: its colours (light and dark), its
typeface Inter, and a slim icon-only bar whose status-bar and gesture-bar
strips share the bar's colour.

- **MakerWorld** tab: browse makerworld.com and sign in as usual. Model pages
  get a green **Print with Bambuddy** button.
- **Printer** tab: Bambuddy's printers page on its own — status, progress,
  AMS, camera — without Bambuddy's menu, so it stays on the printers.
- **Bambuddy** tab: the one-tap print flow. The button opens
  `<address>/makerworld?url=…` here: slice, preview, print. With no flow open
  it shows "Nothing to print yet" with a small **Open Bambuddy** button for
  the full Bambuddy; Back from its first page returns to that screen.
- **Settings** tab (last in the bottom bar): the Bambuddy address, and
  Appearance (System / Light / Dark) for the whole app, both sites included. Back or
  Save returns to the tab you came from.
- **Share** a MakerWorld link to *Bambuddy Handy* from any app to do the same.

Each tab keeps its page and history; logins (cookies) persist. Back goes back
in the visible tab, pull down to refresh.

## Install

1. Build the APK (below) or take a built `app-debug.apk`.
2. Copy it to the phone and open it (allow "install unknown apps" for the file
   manager when asked), or with USB debugging: `adb install -r app-debug.apk`.
3. On first start, the Settings tab opens: enter the address you use for Bambuddy in a
   browser, e.g. `http://192.168.1.10:8000`, tap **Test connection**, then
   **Save**. Change it later on the **Settings** tab.

Updates install over the old version and keep your logins as long as every
build is signed with the same key. Put a keystore at `app/handy.keystore`
(alias `handy`, password `android`; it is git-ignored so the key stays
private) and every build uses it. Without one, Gradle's default debug key signs
the build, which only installs over builds from the same machine. To create
one:

```
keytool -genkeypair -keystore app/handy.keystore -alias handy -keyalg RSA \
  -validity 10000 -storepass android -keypass android -dname CN=BambuddyHandy
```

## Build

Needs JDK 21 and the Android SDK (`local.properties` → `sdk.dir`).

```
cd handy-app
JAVA_HOME=/usr/lib/jvm/java-21-openjdk ./gradlew assembleDebug
```

Output: `app/build/outputs/apk/debug/app-debug.apk`.

## When MakerWorld changes

All MakerWorld-specific code is in `app/src/main/assets/inject.js`:

- `MODEL_PATH` decides which pages get the button (`/models/<id>`, with an
  optional language prefix such as `/en/`).
- The button's look and position are the `style` list (`bottom:88px` keeps it
  above MakerWorld's own bottom bar).
- It sends `location.href`, which includes the selected print profile
  (`#profileId-…`).

Rebuild after editing; nothing else needs to change.

Pull-to-refresh asks `app/src/main/assets/refresh-guard.js` at each touch: it
only refreshes when nothing under the finger is scrolled down and it isn't a
canvas (the G-code preview), since both sites scroll inside panels.

## Security notes

- Plain `http://` is only allowed to the Bambuddy host you configured. The
  network security config has to allow cleartext (the address is chosen at
  runtime), so `MainActivity` blocks every other `http://` request in both
  tabs.
- The `BambuddyHandy.print()` bridge is only attached to the MakerWorld tab,
  only accepts `https://makerworld.com` links, and only acts while that tab is
  on MakerWorld.

## How it fits Bambuddy

The app marks its WebViews in the user agent; this fork's frontend reacts:

- ` BambuddyHandy` (Bambuddy tab): MakerWorld's palette, and light/dark from
  the app's Appearance setting (`html.handy` in `frontend/src/index.css`,
  `ThemeContext.tsx`).
- ` BambuddyHandyEmbedded` (Printer tab): the same, plus no sidebar or header
  (`Layout.tsx`), so the tab is just the printers page.
- The print flow calls `window.BambuddyHandyApp.closeFlow()` when it closes and
  exposes `window.BambuddyHandyBack()`, which the app calls on Back so it steps
  out of the flow's layers (G-code preview, dialogs, settings) one at a time.

Not included: file uploads and downloads inside the WebViews.
