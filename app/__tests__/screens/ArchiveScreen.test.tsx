import { fireEvent, render } from '@testing-library/react-native';
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
});
