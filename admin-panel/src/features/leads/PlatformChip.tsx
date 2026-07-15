import { hexToRgb, platformColor } from '@/shared/ui/charts/chartColors';
import { platformLabel } from '@/shared/lib/platformLabel';

/**
 * Small platform tag colored by the shared per-platform palette. Background is
 * a soft tint of the platform color so it reads as a chip rather than a button.
 * Label casing comes from the shared map (X, LinkedIn, YouTube) — `capitalize`
 * would mis-render those, so the raw string is pre-labeled instead.
 */
export function PlatformChip({ platform }: { readonly platform: string }) {
  const color = platformColor(platform, '#6b7280');
  return (
    <span
      className="inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-bold"
      style={{ color, backgroundColor: `rgba(${hexToRgb(color)}, 0.13)` }}
    >
      {platformLabel(platform)}
    </span>
  );
}
