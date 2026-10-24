# DK Remote

Stream and control a Windows or macOS desktop from a browser. The host captures a display, encodes H.264 in hardware, and sends it over WebRTC. The receiver needs no install. Windows hosts can use NVIDIA, AMD, or Intel graphics. MacBook hosts use Apple's VideoToolbox on Apple silicon or Intel.

## Quick start

### Windows host

Use Windows 10/11 with a current GPU driver and hardware H.264 encoder. In PowerShell, from this folder:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup_windows.ps1
```

The script installs Python 3.12, FFmpeg, cloudflared, and Python packages in `.venv`. It does **not** install a virtual display driver. Package installers may request administrator approval. Start the host:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start_windows.ps1
```

Approve the Windows administrator prompt on the host PC. The Windows host and tunnel start automatically with **Capture display** set to `primary` by default. The host window shows one **Connection link**. Copy that link to the receiving device. Matching the host display to the receiver's pixel size in **Settings → System → Display → Display resolution** gives the sharpest picture, but other sizes still connect.

### MacBook host (Intel or Apple silicon)

Use a macOS version supported by current Homebrew and its Python packages. The pinned PyAV wheel needs macOS 14 or newer on Apple silicon. Install [Homebrew](https://brew.sh/) first. In Terminal, from this folder:

```bash
bash setup_mac.sh
./start_mac.command
```

The script installs Python 3.12, Tk, FFmpeg, cloudflared, and Python packages in `.venv`. It does not install a virtual display app. Allow [**Screen & System Audio Recording**](https://support.apple.com/guide/mac-help/control-access-screen-system-audio-recording-mchld6aa7d23/mac) for the app running FFmpeg and [**Accessibility**](https://support.apple.com/guide/mac-help/allow-accessibility-apps-to-access-your-mac-mh43185/mac) for the app running Python (usually Terminal) in **System Settings → Privacy & Security**. Restart the host after granting permissions. The host window shows one **Connection link** and the selected screen's native pixel size.

For a virtual screen, follow the Mac instructions below. Enter its **Mac screen index** in the host window, click **Refresh display status**, then press **Start host**. Screen index 0 is normally the built-in display. Mac system audio is optional; see the BlackHole instructions below.

### Connect from any browser device

1. Copy the single **Connection link** from the host window and open it on the receiving device. It can be a Chromebook, Mac, Windows PC, tablet, or another device with a compatible browser.
2. Press **Open in about:blank + connect**. The page opens an `about:blank` tab, carries the link into it, and starts connecting. If popups are blocked, allow one for the host page and press the button again. You can also paste the link into a saved copy of the HTML receiver and use the same button.
3. The terminal screen has optional commands: `open`, `connect`, `status`, and `help`. The buttons do the same work, so commands are not required. In `about:blank`, **Connect to host** uses the pasted link directly.
4. Hold **Alt for half a second** to show or hide the ⋮ menu. The **Mouse** lock button is inside that menu. Press **Diagnostics** to view resolution, encoder, frame rate, bitrate, RTT, and decoder information.

Treat the connection link as a password: it contains the tunnel address and a short access proof. The proof is checked by the host before WebRTC setup. The tunnel URL delivers the page and signaling; media uses the WebRTC network path. Networks that block direct UDP may need ICE/TURN configuration, which this repository does not provide.

## Display size and picture quality

The browser reports `screen.width × devicePixelRatio` and `screen.height × devicePixelRatio`, rounded to whole physical pixels. If the host display matches, the receiver uses native pixels. If it differs, the stream still connects and the receiver fits the host picture to its screen, with black space where the aspect ratios differ. Matching resolutions is recommended for the sharpest text. H.264 4:2:0 encoders normally need even encoded dimensions; an odd receiver dimension is allowed, but it cannot be an exact encoded match in this pipeline.

### Windows virtual monitor (optional)

1. To install the driver along with the host dependencies, run `powershell -NoProfile -ExecutionPolicy Bypass -File .\setup_windows.ps1 -VirtualDisplay`. If the host is already set up, you can install just the driver with `winget install --exact --id VirtualDrivers.Virtual-Display-Driver --accept-package-agreements --accept-source-agreements`. In [Virtual Driver Control](https://github.com/VirtualDrivers/Virtual-Driver-Control), add and activate **one** virtual monitor. Use Windows display mode **Extend** so the virtual desktop has its own content.
2. For the sharpest picture, add a mode whose width and height equal the **Receiver** numbers. The driver configuration is normally `C:\VirtualDisplayDriver\vdd_settings.xml`; Virtual Driver Control can edit its available resolutions. Add a refresh rate at least as high as the host's `DK_FPS` (60 by default), then apply the change.
3. Open DK Remote. Once the host starts, press **Stop host**, choose `virtual` under **Capture display**, then press **Start host**. DK Remote remembers that choice and starts with it automatically next time. It finds MttVDD by identity and selects a matching mode when available; otherwise it streams the current virtual display mode.
4. Move the apps you want to stream onto the virtual monitor. In receiver **Diagnostics**, compare **Requested → host mode** and **Resolution**. Matching sizes give the sharpest picture; a different mode still streams.

The Windows encoder order is NVIDIA NVENC, AMD AMF, Media Foundation hardware H.264, then Intel QSV. The host tries the next available encoder if one fails to start. Media Foundation can use a compatible hardware encoder on AMD or Intel systems. To choose one explicitly, set `$env:DK_ENCODER='amf'`, `'mf'`, `'qsv'`, or `'nvenc'` in PowerShell before starting. For a physical display other than the primary one, choose `manual` and set its DXGI adapter/output indexes with `DK_DISPLAY_ADAPTER_INDEX` and `DK_DISPLAY_OUTPUT_INDEX`. The `DK_CAPTURE_DISPLAY` environment variable can also select `primary`, `virtual`, or `manual` and overrides the saved host-window choice.

### Mac virtual screen

The host captures the selected macOS display and does not change its resolution automatically. Use a physical display, or create a virtual screen with [BetterDisplay](https://github.com/waydabber/BetterDisplay): **App menu → Tools → Displays and Virtual Screens → Create New Virtual Screen** (or **Settings → Displays → Overview**). For the sharpest image, choose or add the receiver's pixel dimensions. Check the **native pixel size** shown in DK Remote, because a macOS “looks like”/HiDPI setting can have different physical pixels. If needed, turn off HiDPI for that virtual screen and choose a native mode.

Select the virtual screen's index in DK Remote and refresh its status. The host reports the screen size at connection time and the receiver fits the picture if it differs. Arrange the virtual screen in macOS Displays settings and move the desired apps onto it. Some macOS/BetterDisplay combinations do not offer very small custom modes; an exact match may therefore be unavailable. Changing the screen or mode during a session requires reconnecting.

### Optional Mac system audio

macOS does not offer the Windows WASAPI speaker loopback used by this host. To send Mac system audio, install [BlackHole 2ch](https://github.com/ExistentialAudio/BlackHole) with `brew install --cask blackhole-2ch`, then restart the Mac if prompted. In **Audio MIDI Setup**, create a **Multi-Output Device** containing your speakers/headphones and **BlackHole 2ch**, enable drift correction for BlackHole, and select that Multi-Output Device as the Mac's sound output. [BlackHole's guide](https://github.com/ExistentialAudio/BlackHole/wiki/Getting-Started%3A-Creating-a-Multi-Output-Device) shows each step. Restart DK Remote, turn on **Send host speaker audio**, and grant Microphone access to Terminal/Python if macOS requests it. The input name defaults to `BlackHole 2ch`; set `DK_MAC_AUDIO_DEVICE` before starting if you renamed it. Without BlackHole, the host leaves Mac audio off while video and input still work.

## Latency and image quality

There is no single fixed “theoretical minimum” for an interactive stream. Capture scheduling, hardware encode and decode, network RTT/jitter, browser buffering, and display refresh each add time. Removing all buffering can cause stalls and lost frames; pushing compression harder can lower image quality. DK Remote uses native host capture, hardware H.264, no B frames, short host queues, low delay muxing, and an optional fast WebCodecs receiver. **Stable quality** keeps the configured bitrate budget; **Adaptive bandwidth** can reduce it when the network cannot sustain it. For the cleanest picture, use **Sharp** in the host window and **Stable quality** in the browser.

H.264 is lossy and normally uses 4:2:0 chroma. When display sizes differ, the receiver scales the picture to fit. The default sharp budget is 14 Mbps target / 20 Mbps peak. Higher resolutions or detailed motion can benefit from more bitrate, provided the connection has room for it. Before starting the host, for example:

```powershell
$env:DK_BITRATE='30M'; $env:DK_MAXRATE='40M'  # Windows PowerShell
```

```bash
DK_BITRATE=30M DK_MAXRATE=40M ./start_mac.command  # macOS
```

Increase both while watching **Diagnostics** for packet loss, bitrate, dropped frames, and RTT. Set `DK_FPS` no higher than the selected display's refresh rate or the receiver's display rate. For the shortest network path on a trusted LAN, start the host with `DK_BIND_HOST=0.0.0.0`. To make a LAN connection link, run `.\.venv\Scripts\python.exe -c "import remote_host; h='192.168.1.2:8765'; print('http://'+h+'/#'+remote_host.connection_code(h))"` on Windows or replace the interpreter with `./.venv/bin/python` on macOS. Replace the sample IP with the host's LAN IP. Firewall access may be needed. The cloudflared link is convenient across networks but does not guarantee a direct or low RTT media path. Browser WebCodecs and fast playback depend on browser support and secure context; compare **Playback: standard** and **Playback: fast** on the receiver.

Diagnostics estimates sender timeline to display, receiver queueing, and network RTT. It cannot directly measure capture and encode or true input-to-pixel latency; that needs an external high-speed camera or similar timing setup. “Theoretical limit without sacrificing quality” cannot be guaranteed for every GPU, Mac, browser, display, and network. The practical goal is the lowest measured latency at a resolution and bitrate the connection holds without visible quality loss or frame drops.

## Troubleshooting

- **Tunnel unavailable or host is down:** Check that DK Remote is running and showing a fresh connection link. Open that link again, then use **Open in about:blank + connect**. A dead link cannot load the receiver page at all; a saved HTML receiver can still display the error.
- **Connection link rejected:** Copy a fresh link from the host window. The link contains the access proof; no separate secret entry is needed.
- **Windows: picture is scaled:** Match the primary display to the receiver's width and height for sharper text, or use the optional virtual monitor. A mismatch still streams.
- **Windows: virtual display not found:** Activate one MttVDD monitor in Virtual Driver Control. If its exact mode is unavailable, the host streams its current mode.
- **Mac: picture is scaled:** Read the native pixel size in the host window and select a matching mode for sharper text. HiDPI “looks like” values are not the native pixel count.
- **Mac: blank or black capture / input ignored:** Grant Screen & System Audio Recording and Accessibility in Privacy & Security to Terminal, Python, and FFmpeg as listed, then restart the host/Terminal.
- **Encoder failed:** Check `ffmpeg -hide_banner -encoders` for `h264_nvenc`, `h264_amf`, `h264_mf`, `h264_qsv`, or `h264_videotoolbox`. Update the GPU driver or FFmpeg. The host logs each attempt and rejects software fallback.
- **Connection fails after signaling:** Check that both devices permit WebRTC UDP and STUN. The tunnel only carries signaling; this build has no TURN relay.
- **Choppy video:** Check Diagnostics for lost packets, queue skips, bitrate, and decode time. Try the direct LAN path, lower `DK_BITRATE`/`DK_MAXRATE` if the network is saturated, or compare receiver playback modes. Lowering bitrate can reduce image quality.

The host keeps its underlying secret in `remote_secret.txt` and UI choices, including the Windows display choice, in `remote_settings.json`. Keep the secret and every connection link private. Python packages install into `.venv` in this folder.
