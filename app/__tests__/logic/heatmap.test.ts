import { buildHeatmapCells } from '../../src/logic/heatmap';
import type { Reel } from '../../src/types';

function reel(date: string): Reel {
  return { id: `reel-${date}`, date, s3Key: `device/${date}.mp4`, durationSec: 90, style: 'clean' };
}

describe('buildHeatmapCells', () => {
  it('marks each day in the range as having a reel or not', () => {
    const cells = buildHeatmapCells(
      [reel('2026-07-01'), reel('2026-07-03')],
      new Date('2026-07-01T00:00:00.000Z'),
      new Date('2026-07-03T00:00:00.000Z'),
    );

    expect(cells).toEqual([
      { date: '2026-07-01', hasReel: true },
      { date: '2026-07-02', hasReel: false },
      { date: '2026-07-03', hasReel: true },
    ]);
  });

  it('returns a single cell when start and end are the same day', () => {
    const cells = buildHeatmapCells([], new Date('2026-07-01T00:00:00.000Z'), new Date('2026-07-01T00:00:00.000Z'));
    expect(cells).toEqual([{ date: '2026-07-01', hasReel: false }]);
  });
});
