import React from 'react';
import { Image, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';

import type { TimelineSlot } from '../logic/timeline';
import { colors, spacing } from '../theme';

interface TimelineScrubberProps {
  slots: TimelineSlot[];
  thumbnails?: Record<number, string>;
  onSlotLongPress: (slot: TimelineSlot) => void;
}

export function TimelineScrubber({ slots, thumbnails = {}, onSlotLongPress }: TimelineScrubberProps) {
  return (
    <ScrollView horizontal testID="timeline-scrubber" style={styles.strip}>
      {slots.map((slot) => (
        <Pressable
          key={slot.startMinute}
          testID={`slot-${slot.startMinute}`}
          accessibilityLabel={`Timeline slot ${formatMinute(slot.startMinute)}`}
          onLongPress={() => onSlotLongPress(slot)}
        >
          <View style={[styles.slot, slot.segment ? styles.occupied : null]}>
            {thumbnails[slot.startMinute] ? (
              <Image
                testID={`thumb-${slot.startMinute}`}
                source={{ uri: thumbnails[slot.startMinute] }}
                style={styles.thumb}
                accessibilityLabel={`Preview at ${formatMinute(slot.startMinute)}`}
              />
            ) : null}
            {slot.isFlagged ? <Text testID={`flag-${slot.startMinute}`}>🚩</Text> : null}
          </View>
        </Pressable>
      ))}
    </ScrollView>
  );
}

function formatMinute(startMinute: number): string {
  const h = String(Math.floor(startMinute / 60)).padStart(2, '0');
  const m = String(startMinute % 60).padStart(2, '0');
  return `${h}:${m}`;
}

const styles = StyleSheet.create({
  strip: { flexGrow: 0 },
  slot: {
    width: 48,
    height: 64,
    marginRight: spacing.xs,
    borderRadius: 6,
    backgroundColor: colors.surface,
    alignItems: 'center',
    justifyContent: 'center',
    overflow: 'hidden',
  },
  occupied: { borderWidth: 1, borderColor: colors.accent },
  thumb: { position: 'absolute', width: '100%', height: '100%' },
});
