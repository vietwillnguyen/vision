import { fireEvent, render, screen } from '@testing-library/react-native';
import React from 'react';

import { ArchiveScreen } from '../../src/screens/ArchiveScreen';
import type { HeatmapCell } from '../../src/logic/heatmap';

const cells: HeatmapCell[] = [
  { date: '2026-07-01', hasReel: true },
  { date: '2026-07-02', hasReel: false },
];

describe('ArchiveScreen', () => {
  it('calls onDayPress with the date of a pressed cell', () => {
    const onDayPress = jest.fn();
    const { getByTestId } = render(<ArchiveScreen cells={cells} onDayPress={onDayPress} />);

    fireEvent.press(getByTestId('heatmap-cell-2026-07-01'));

    expect(onDayPress).toHaveBeenCalledWith('2026-07-01');
  });

  it('labels each cell for accessibility', () => {
    render(
      <ArchiveScreen
        cells={[
          { date: '2026-07-17', hasReel: false },
          { date: '2026-07-18', hasReel: true },
        ]}
        onDayPress={jest.fn()}
      />,
    );
    expect(screen.getByLabelText('Day 2026-07-18, reel available')).toBeTruthy();
    expect(screen.getByLabelText('Day 2026-07-17, no reel')).toBeTruthy();
  });
});
