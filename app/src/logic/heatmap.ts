import type { Reel } from '../types';

export interface HeatmapCell {
  date: string;
  hasReel: boolean;
}

export function buildHeatmapCells(reels: Reel[], rangeStart: Date, rangeEnd: Date): HeatmapCell[] {
  const reelDates = new Set(reels.map((r) => r.date));
  const cells: HeatmapCell[] = [];
  const cursor = new Date(rangeStart);

  while (cursor.getTime() <= rangeEnd.getTime()) {
    const dateStr = cursor.toISOString().slice(0, 10);
    cells.push({ date: dateStr, hasReel: reelDates.has(dateStr) });
    cursor.setUTCDate(cursor.getUTCDate() + 1);
  }

  return cells;
}
