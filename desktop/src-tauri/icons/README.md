# App icons — NOT checked in (build blocker)

> **SCAFFOLD NOTE:** no icon assets are committed here. `tauri.conf.json` references
> `icons/32x32.png`, `icons/128x128.png`, `icons/128x128@2x.png`, `icons/icon.icns`,
> `icons/icon.ico` — none of which exist yet. **`cargo tauri build` (and even
> `cargo tauri dev` bundling) will FAIL until a real icon set is generated.**

## How to generate

From a single high-resolution square source PNG (>= 1024x1024, transparent background):

```bash
cargo tauri icon path/to/aizu-worker-source.png
```

This writes the full set (`.png` sizes, `icon.icns` for macOS, `icon.ico` for Windows)
into this directory, matching the paths declared in `tauri.conf.json`.

## Brand

Product name is **AIZU Worker** (rebranded ReelRadar → AIZU, 合図 / "signal to act").
Use the AIZU "ping" mark on the "Ink × Lime" palette to stay consistent with the web panel.
