# Asset Library — inline SVG (deterministic, no network)

**How to use:** Every asset below is self-contained inline SVG markup (no external refs, no remote `<image>`, no scripts). Copy the `<svg>...</svg>` block straight into `index.html` where you want it. Size it with the SVG's own `width`/`height` attributes or by wrapping it in a sized element (e.g. `<span style="display:inline-block;width:48px">…</span>` and adding `width="100%" height="100%"` to the svg). Each icon fills its `viewBox`, so scaling to ~40–64px stays crisp. Recolor monochrome marks by editing the `fill` (the brand-color marks below already carry their own fills; override only if you want a mono treatment). All gradients use unique `id`s so multiple assets can coexist on one page.

---

## google-g

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" width="48" height="48" role="img" aria-label="Google">
  <path fill="#4285F4" d="M47.5 24.5c0-1.6-.15-3.15-.42-4.64H24v9.4h13.2c-.57 3.07-2.3 5.67-4.9 7.42v6.17h7.93C44.9 38.6 47.5 32.1 47.5 24.5z"/>
  <path fill="#34A853" d="M24 48c6.62 0 12.18-2.2 16.24-5.94l-7.93-6.17c-2.2 1.48-5.02 2.35-8.31 2.35-6.39 0-11.8-4.32-13.73-10.12H2.06v6.36C6.1 42.62 14.4 48 24 48z"/>
  <path fill="#FBBC05" d="M10.27 28.12c-.49-1.48-.77-3.06-.77-4.62s.28-3.14.77-4.62v-6.36H2.06A23.9 23.9 0 0 0 0 23.5c0 3.87.93 7.53 2.06 10.98l8.21-6.36z"/>
  <path fill="#EA4335" d="M24 9.5c3.6 0 6.83 1.24 9.38 3.66l7.03-7.03C36.17 2.13 30.61 0 24 0 14.4 0 6.1 5.38 2.06 13.5l8.21 6.36C12.2 13.82 17.61 9.5 24 9.5z"/>
</svg>
```

## instagram

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" width="48" height="48" role="img" aria-label="Instagram">
  <defs>
    <linearGradient id="igGrad" x1="8%" y1="92%" x2="92%" y2="8%">
      <stop offset="0" stop-color="#FEDA75"/>
      <stop offset="0.25" stop-color="#FA7E1E"/>
      <stop offset="0.5" stop-color="#D62976"/>
      <stop offset="0.75" stop-color="#962FBF"/>
      <stop offset="1" stop-color="#4F5BD5"/>
    </linearGradient>
  </defs>
  <rect x="2" y="2" width="44" height="44" rx="13" fill="url(#igGrad)"/>
  <rect x="12" y="12" width="24" height="24" rx="7" fill="none" stroke="#fff" stroke-width="3"/>
  <circle cx="24" cy="24" r="6" fill="none" stroke="#fff" stroke-width="3"/>
  <circle cx="32.5" cy="15.5" r="2.2" fill="#fff"/>
</svg>
```

## facebook

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" width="48" height="48" role="img" aria-label="Facebook">
  <circle cx="24" cy="24" r="24" fill="#1877F2"/>
  <path fill="#fff" d="M28.6 25.6l.85-5.53h-5.3v-3.59c0-1.51.74-2.99 3.11-2.99h2.41V8.79s-2.19-.37-4.28-.37c-4.37 0-7.22 2.65-7.22 7.44v4.21h-4.86v5.53h4.86V39h5.98V25.6z"/>
</svg>
```

## twitter-x

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" width="48" height="48" role="img" aria-label="X">
  <rect x="2" y="2" width="44" height="44" rx="11" fill="#000"/>
  <path fill="#fff" d="M27.8 21.9L37 11.5h-2.6l-8 9-6.4-9H12l9.6 13.6L12 36.5h2.6l8.4-9.5 6.7 9.5H37L27.8 21.9zm-3 3.4l-1-1.4-7.7-10.8h3.8l6.2 8.8 1 1.4 8.1 11.4h-3.8l-6.6-9.4z"/>
</svg>
```

## twitter-bird

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" width="48" height="48" role="img" aria-label="Twitter">
  <circle cx="24" cy="24" r="24" fill="#1DA1F2"/>
  <path fill="#fff" d="M38 16.3c-1.03.46-2.14.77-3.3.91a5.77 5.77 0 0 0 2.53-3.18 11.5 11.5 0 0 1-3.65 1.4 5.75 5.75 0 0 0-9.79 5.24 16.32 16.32 0 0 1-11.85-6.01 5.75 5.75 0 0 0 1.78 7.67 5.7 5.7 0 0 1-2.6-.72v.07a5.75 5.75 0 0 0 4.61 5.64 5.77 5.77 0 0 1-2.6.1 5.76 5.76 0 0 0 5.37 3.99A11.54 11.54 0 0 1 10 39.2a16.28 16.28 0 0 0 8.82 2.58c10.58 0 16.37-8.77 16.37-16.37l-.02-.75A11.7 11.7 0 0 0 38 16.3z"/>
</svg>
```

## yelp

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" width="48" height="48" role="img" aria-label="Yelp">
  <rect x="1" y="1" width="46" height="46" rx="10" fill="#fff"/>
  <g fill="#D32323">
    <path d="M22.4 24.7l-8.9-4.3c-1-.5-1.05-1.9-.1-2.5l7.7-4.9c1-.63 2.3.14 2.2 1.32l-.6 10.2c-.06 1.15-1.28 1.85-2.3 1.18z"/>
    <path d="M24.9 26.9l3.4-4.2c.7-.87 2.06-.66 2.47.38l2.3 5.85c.4 1.04-.5 2.1-1.6 1.9l-6.1-1.02c-1.2-.2-1.7-1.72-.87-2.66z" transform="translate(0.5 0)"/>
    <path d="M24 30.3c1.1-.34 2.2.56 2.06 1.7l-.72 6.1c-.14 1.15-1.5 1.66-2.36.88l-3-2.7c-.86-.78-.6-2.2.47-2.6z" transform="translate(0 -0.5)"/>
    <path d="M20.9 27.6c.9.72.7 2.14-.36 2.6l-5.7 2.46c-1.07.46-2.2-.42-2.06-1.57l.8-6.1c.15-1.14 1.5-1.63 2.35-.84z"/>
    <path d="M27 22.2c-.9-.7-.68-2.13.4-2.57l6.5-2.7c1.08-.44 2.2.47 2.03 1.62l-.95 6.35c-.17 1.14-1.53 1.6-2.37.8z"/>
  </g>
</svg>
```

## tripadvisor

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 48" width="64" height="48" role="img" aria-label="Tripadvisor">
  <ellipse cx="32" cy="26" rx="30" ry="20" fill="#000" opacity="0"/>
  <path fill="#00AA6C" d="M32 8c-6.2 0-11.8 1.5-15.9 3.6H4l4.3 4.7A11.7 11.7 0 0 0 20 36a11.66 11.66 0 0 0 9.1-4.4l2.9 3.2 2.9-3.2A11.66 11.66 0 0 0 44 36a11.7 11.7 0 0 0 11.7-19.7L60 11.6H47.9C43.8 9.5 38.2 8 32 8z"/>
  <circle cx="20" cy="24" r="9.5" fill="#fff"/>
  <circle cx="44" cy="24" r="9.5" fill="#fff"/>
  <circle cx="20" cy="24" r="4.6" fill="#000"/>
  <circle cx="44" cy="24" r="4.6" fill="#000"/>
  <circle cx="18.4" cy="22.4" r="1.5" fill="#fff"/>
  <circle cx="42.4" cy="22.4" r="1.5" fill="#fff"/>
  <path d="M32 13.5c3.2 0 6 .6 8 1.6-3.3 1.4-5.7 4.5-6.4 8.2-.5-.3-1-.4-1.6-.4s-1.1.1-1.6.4c-.7-3.7-3.1-6.8-6.4-8.2 2-1 4.8-1.6 8-1.6z" fill="#00AA6C"/>
</svg>
```

## trustpilot

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" width="48" height="48" role="img" aria-label="Trustpilot">
  <path fill="#00B67A" d="M24 3l5.6 16.1H47l-14 10.9 5.4 16.5L24 35.6 9.6 46.5 15 30 1 19.1h17.4z"/>
  <path fill="#005128" d="M24 35.6l8.7-2.2 2.7 8.6z" opacity=".85"/>
</svg>
```

## booking

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" width="48" height="48" role="img" aria-label="Booking.com">
  <rect x="2" y="2" width="44" height="44" rx="8" fill="#003580"/>
  <path fill="#fff" d="M15 12h9.2c4.4 0 7.3 2.3 7.3 6.1 0 2.3-1.1 3.9-2.8 4.8 2.4.8 3.9 2.7 3.9 5.4 0 4.3-3.3 6.7-8.2 6.7H15zm5.2 4.3v5.1h3.4c1.9 0 3-.98 3-2.6 0-1.6-1.1-2.5-3-2.5zm0 9v5.6h3.9c2.1 0 3.3-1.02 3.3-2.8 0-1.8-1.2-2.8-3.4-2.8z"/>
  <circle cx="36.5" cy="33.5" r="2.8" fill="#00BAFC"/>
</svg>
```

## whatsapp

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" width="48" height="48" role="img" aria-label="WhatsApp">
  <path fill="#25D366" d="M24 2C11.85 2 2 11.85 2 24c0 3.87 1.02 7.66 2.96 11L2 46l11.3-2.9A21.9 21.9 0 0 0 24 46c12.15 0 22-9.85 22-22S36.15 2 24 2z"/>
  <path fill="#fff" d="M18.1 13.6c-.4-.9-.82-.92-1.2-.94l-1.02-.01c-.35 0-.93.13-1.42.66-.49.53-1.86 1.82-1.86 4.43s1.9 5.14 2.17 5.5c.27.35 3.68 5.9 9.1 8.03 4.5 1.77 5.42 1.42 6.4 1.33.98-.09 3.16-1.29 3.6-2.54.44-1.25.44-2.32.31-2.54-.13-.22-.49-.35-1.02-.62-.53-.27-3.16-1.56-3.65-1.74-.49-.18-.85-.27-1.2.27-.35.53-1.38 1.74-1.69 2.1-.31.35-.62.4-1.15.13-.53-.27-2.25-.83-4.29-2.64-1.59-1.42-2.66-3.17-2.97-3.7-.31-.53-.03-.82.23-1.09.24-.24.53-.62.8-.93.26-.31.35-.53.53-.89.18-.35.09-.66-.04-.93-.13-.27-1.16-2.9-1.6-3.95z"/>
</svg>
```

## google-search-pill

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 360 56" width="360" height="56" role="img" aria-label="Google search bar">
  <rect x="1" y="1" width="358" height="54" rx="27" fill="#fff" stroke="#DFE1E5" stroke-width="1.5"/>
  <g transform="translate(20 15) scale(0.54)">
    <path fill="#4285F4" d="M47.5 24.5c0-1.6-.15-3.15-.42-4.64H24v9.4h13.2c-.57 3.07-2.3 5.67-4.9 7.42v6.17h7.93C44.9 38.6 47.5 32.1 47.5 24.5z"/>
    <path fill="#34A853" d="M24 48c6.62 0 12.18-2.2 16.24-5.94l-7.93-6.17c-2.2 1.48-5.02 2.35-8.31 2.35-6.39 0-11.8-4.32-13.73-10.12H2.06v6.36C6.1 42.62 14.4 48 24 48z"/>
    <path fill="#FBBC05" d="M10.27 28.12c-.49-1.48-.77-3.06-.77-4.62s.28-3.14.77-4.62v-6.36H2.06A23.9 23.9 0 0 0 0 23.5c0 3.87.93 7.53 2.06 10.98l8.21-6.36z"/>
    <path fill="#EA4335" d="M24 9.5c3.6 0 6.83 1.24 9.38 3.66l7.03-7.03C36.17 2.13 30.61 0 24 0 14.4 0 6.1 5.38 2.06 13.5l8.21 6.36C12.2 13.82 17.61 9.5 24 9.5z"/>
  </g>
  <text x="60" y="34" font-family="Arial, Helvetica, sans-serif" font-size="17" fill="#9AA0A6">Search…</text>
  <g transform="translate(320 18)" fill="none" stroke="#9AA0A6" stroke-width="2.2" stroke-linecap="round">
    <circle cx="8" cy="8" r="7"/>
    <line x1="13.5" y1="13.5" x2="20" y2="20"/>
  </g>
</svg>
```

## owner-avatar

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96" width="96" height="96" role="img" aria-label="Business owner">
  <defs>
    <clipPath id="ownerClip"><circle cx="48" cy="48" r="48"/></clipPath>
  </defs>
  <g clip-path="url(#ownerClip)">
    <rect width="96" height="96" fill="#E8EEF5"/>
    <!-- shoulders / shirt -->
    <path d="M14 96c0-15 15-24 34-24s34 9 34 24z" fill="#3C6E9F"/>
    <path d="M40 68h16v12l-8 6-8-6z" fill="#F0C9A8"/>
    <!-- collar -->
    <path d="M40 74l8 6 8-6 6 4-14 8-14-8z" fill="#F4F6F8"/>
    <!-- neck -->
    <rect x="42" y="60" width="12" height="14" rx="6" fill="#F0C9A8"/>
    <!-- head -->
    <circle cx="48" cy="44" r="19" fill="#F5D0AE"/>
    <!-- ears -->
    <circle cx="29.5" cy="44" r="3.4" fill="#F0C9A8"/>
    <circle cx="66.5" cy="44" r="3.4" fill="#F0C9A8"/>
    <!-- hair -->
    <path d="M29 42c0-12 8-19 19-19s19 7 19 19c0-5-3-7-6-7 0-3-5-6-13-6s-13 3-13 6c-3 0-6 2-6 7z" fill="#5A4633"/>
    <!-- eyebrows -->
    <path d="M38 40q4-2.4 8 0" stroke="#5A4633" stroke-width="1.8" fill="none" stroke-linecap="round"/>
    <path d="M50 40q4-2.4 8 0" stroke="#5A4633" stroke-width="1.8" fill="none" stroke-linecap="round"/>
    <!-- eyes -->
    <circle cx="42" cy="44.5" r="2.1" fill="#3B3128"/>
    <circle cx="54" cy="44.5" r="2.1" fill="#3B3128"/>
    <!-- nose -->
    <path d="M48 46v4.5l-2 1" stroke="#D9A87F" stroke-width="1.6" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
    <!-- friendly smile -->
    <path d="M42 54q6 5 12 0" stroke="#B96A56" stroke-width="2" fill="none" stroke-linecap="round"/>
    <!-- cheeks -->
    <circle cx="38" cy="50" r="2.6" fill="#F3B79A" opacity=".55"/>
    <circle cx="58" cy="50" r="2.6" fill="#F3B79A" opacity=".55"/>
  </g>
</svg>
```

## phone-frame

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 220 440" width="220" height="440" role="img" aria-label="Phone frame">
  <!-- device body -->
  <rect x="6" y="6" width="208" height="428" rx="34" fill="#111418"/>
  <rect x="6" y="6" width="208" height="428" rx="34" fill="none" stroke="#2A2E35" stroke-width="2"/>
  <!-- screen (layer UI mock inside this rect's bounds: x14 y18 w192 h404) -->
  <rect x="14" y="18" width="192" height="404" rx="24" fill="#FFFFFF"/>
  <!-- notch / dynamic island -->
  <rect x="82" y="24" width="56" height="14" rx="7" fill="#111418"/>
  <!-- status bar hints -->
  <text x="30" y="50" font-family="Arial, Helvetica, sans-serif" font-size="11" font-weight="700" fill="#111418">9:41</text>
  <g transform="translate(160 42)" fill="#111418">
    <rect x="0" y="0" width="18" height="9" rx="2" fill="none" stroke="#111418" stroke-width="1.2"/>
    <rect x="1.5" y="1.5" width="13" height="6" rx="1" fill="#111418"/>
    <rect x="18.5" y="3" width="2" height="3" rx="1" fill="#111418"/>
  </g>
  <!-- home indicator -->
  <rect x="82" y="408" width="56" height="4" rx="2" fill="#111418" opacity=".35"/>
</svg>
```
