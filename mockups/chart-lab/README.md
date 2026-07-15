# Chart Lab — Custom SVG vs Recharts

A side-by-side comparison for the admin-panel rebuild. Both pages render the
**exact same dataset** (`assets/dataset.js`) with the **same Pulse styling**
(`assets/lab.css`) — only the chart-rendering engine differs.

Open either file directly in a browser:

- **`custom-svg.html`** — charts hand-built as SVG in the Pulse idiom (no chart
  library). Pixel-faithful to the Pulse mockup; full control over animation,
  tooltips, donut gaps, crosshair.
- **`recharts.html`** — the same charts via **Recharts**, restyled with Pulse
  tokens (loads React + Recharts + Babel from unpkg CDN, so it needs internet).

Use the switch in the top bar to flip between them, and the ☀/☾ toggle to compare
the **light and dark themes** (dark palette derived from real dark analytics
dashboards on Mobbin — Lovable, Posh, Mixpanel, StackAI).

## Charts shown (identical in both)

KPI sparklines · multi-line (toggleable legend, hover crosshair) · area/gradient
(CPL trend) · donut (spend by channel) · grouped bars (this vs previous).

## Trade-offs

| | Custom SVG | Recharts |
|---|---|---|
| Visual fidelity to Pulse | Exact | Very close |
| Animations (draw-in, donut sweep) | Full | Library defaults |
| Bundle weight | ~0 (no dep) | +Recharts |
| Maintenance | We own the code | Library-maintained |
| Exotic charts (gauge, heatmap, funnel) | Same approach | Still need custom SVG |

> Note: Pulse's gauge, hourly heatmap, and funnel have no Recharts equivalent —
> those stay hand-built SVG regardless of which engine wins for the standard charts.
