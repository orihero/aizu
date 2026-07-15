import type { CSSProperties } from 'react';
import { useChartPalette } from '@/shared/hooks/useChartPalette';

/** Light foreground used for ALL tooltip text. The tooltip background is dark in
 *  both themes, so item/label text must be light — and crucially must NOT inherit
 *  the per-series color (e.g. light-theme `brand` is #16161a, identical to the
 *  tooltip background, which renders the text invisible). */
const TOOLTIP_TEXT = '#f5f6f8';

export interface TooltipProps {
  readonly contentStyle: CSSProperties;
  readonly itemStyle: CSSProperties;
  readonly labelStyle: CSSProperties;
}

/** Shared themed Recharts tooltip props (factored out of every chart).
 *  Spread onto <Tooltip {...useTooltipProps()} />. */
export function useTooltipProps(): TooltipProps {
  const palette = useChartPalette();
  return {
    contentStyle: {
      background: palette.tooltipBg,
      border: `1px solid ${palette.tooltipBorder}`,
      borderRadius: 8,
      fontSize: 11,
      color: TOOLTIP_TEXT,
    },
    // itemStyle overrides Recharts' default of coloring each row by its series color.
    itemStyle: { color: TOOLTIP_TEXT },
    labelStyle: { color: TOOLTIP_TEXT },
  };
}
