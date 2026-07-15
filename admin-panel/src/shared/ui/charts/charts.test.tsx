import { describe, expect, test } from 'vitest';
import { render } from '@testing-library/react';
import type { ReactNode } from 'react';
import { ThemeProvider } from '@/shared/hooks/useTheme';
import {
  AreaTrendChart,
  DonutChart,
  FunnelChartView,
  GoalGauge,
  GroupedBarChart,
  HeatGrid,
  MiniBars,
  MultiLineChart,
  Sparkline,
} from './index';

function wrap(ui: ReactNode) {
  // Charts read theme colors via useChartPalette → ThemeProvider.
  return render(<ThemeProvider>{ui}</ThemeProvider>);
}

describe('chart components mount without throwing', () => {
  test('Sparkline', () => {
    expect(() => wrap(<Sparkline data={[1, 2, 3, 2, 4]} />)).not.toThrow();
  });
  test('MiniBars', () => {
    expect(() => wrap(<MiniBars data={[0.4, 0.3, 0.5]} />)).not.toThrow();
  });
  test('GoalGauge', () => {
    const { container } = wrap(<GoalGauge pct={42} label="12/50" />);
    expect(container.textContent).toContain('42%');
  });
  test('GroupedBarChart', () => {
    expect(() =>
      wrap(<GroupedBarChart data={[{ category: 'instagram', current: 5, previous: 3 }]} />),
    ).not.toThrow();
  });
  test('FunnelChartView', () => {
    expect(() =>
      wrap(<FunnelChartView stages={[{ name: 'Reels', value: 100 }, { name: 'Leads', value: 10 }]} />),
    ).not.toThrow();
  });
  test('MultiLineChart', () => {
    expect(() =>
      wrap(<MultiLineChart labels={['a', 'b']} series={[{ name: 'instagram', values: [1, 2] }]} byPlatform />),
    ).not.toThrow();
  });
  test('AreaTrendChart', () => {
    expect(() => wrap(<AreaTrendChart labels={['a', 'b']} values={[1, 2]} name="CPL" />)).not.toThrow();
  });
  test('DonutChart', () => {
    const { container } = wrap(
      <DonutChart data={[{ name: 'match', value: 1 }]} centerValue="$0.14" centerLabel="spent" />,
    );
    expect(container.textContent).toContain('$0.14');
  });
  test('HeatGrid renders 24 cells with an hour label', () => {
    const { container, getByText } = wrap(<HeatGrid values={Array.from({ length: 24 }, (_, h) => h)} />);
    expect(container.querySelector('[role="img"]')?.children).toHaveLength(24);
    expect(getByText('12a')).toBeInTheDocument();
  });
});
