import React from 'react';
import { Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';

import type { HeatmapCell } from '../logic/heatmap';
import { colors, spacing } from '../theme';

interface ArchiveScreenProps {
  cells: HeatmapCell[];
  onDayPress: (date: string) => void;
}

export function ArchiveScreen({ cells, onDayPress }: ArchiveScreenProps) {
  return (
    <ScrollView testID="archive-heatmap" style={styles.container} contentContainerStyle={styles.grid}>
      {cells.map((cell) => (
        <Pressable
          key={cell.date}
          testID={`heatmap-cell-${cell.date}`}
          accessibilityLabel={`Day ${cell.date}, ${cell.hasReel ? 'reel available' : 'no reel'}`}
          onPress={() => onDayPress(cell.date)}
        >
          <View style={[styles.cell, cell.hasReel ? styles.hasReel : null]}>
            <Text style={cell.hasReel ? styles.dotActive : styles.dot}>{cell.hasReel ? '●' : '○'}</Text>
            <Text style={styles.date}>{cell.date.slice(5)}</Text>
          </View>
        </Pressable>
      ))}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.background },
  grid: { flexDirection: 'row', flexWrap: 'wrap', padding: spacing.md },
  cell: {
    width: 56,
    height: 56,
    margin: spacing.xs,
    borderRadius: 8,
    backgroundColor: colors.surface,
    alignItems: 'center',
    justifyContent: 'center',
  },
  hasReel: { borderWidth: 1, borderColor: colors.accent },
  dot: { color: colors.textMuted },
  dotActive: { color: colors.accent },
  date: { color: colors.textMuted, fontSize: 10, marginTop: 2 },
});
