import React from 'react';
import { Pressable, ScrollView, Text } from 'react-native';

import type { HeatmapCell } from '../logic/heatmap';

interface ArchiveScreenProps {
  cells: HeatmapCell[];
  onDayPress: (date: string) => void;
}

export function ArchiveScreen({ cells, onDayPress }: ArchiveScreenProps) {
  return (
    <ScrollView testID="archive-heatmap">
      {cells.map((cell) => (
        <Pressable
          key={cell.date}
          testID={`heatmap-cell-${cell.date}`}
          onPress={() => onDayPress(cell.date)}
        >
          <Text>{cell.hasReel ? '●' : '○'}</Text>
        </Pressable>
      ))}
    </ScrollView>
  );
}
