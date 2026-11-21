import React from 'react';
import { Image, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import Svg, { Path } from 'react-native-svg';

import { formatMinuteOfDay, type TimelineSlot } from '../logic/timeline';
import { colors, spacing } from '../theme';

interface TimelineScrubberProps {
  slots: TimelineSlot[];
  thumbnails?: Record<number, string>;
  onSlotPress?: (slot: TimelineSlot) => void;
  onSlotLongPress: (slot: TimelineSlot) => void;
  selectedMinute?: number | null;
}

/**
 * A horizontal strip of the day's clips in time order.
 *
 * Recorded minutes are tiles with a thumbnail and their start time; minutes
 * with nothing recorded are thin ticks, so gaps between moments read as time
 * passing without pushing the clips off screen. Each hour is labelled.
 */
export function TimelineScrubber({
  slots,
  thumbnails = {},
  onSlotPress,
  onSlotLongPress,
  selectedMinute = null,
}: TimelineScrubberProps) {
  return (
    <ScrollView
      horizontal
      testID="timeline-scrubber"
      style={styles.strip}
      contentContainerStyle={styles.content}
    >
      {slots.map((slot) => {
        const label = formatMinuteOfDay(slot.startMinute);
        if (!slot.segment) {
          if (slot.startMinute % 60 === 0) {
            return (
              <View key={slot.startMinute} testID={`slot-${slot.startMinute}`} style={styles.hour}>
                <View style={styles.hourLine} />
                <Text style={styles.hourLabel}>{label}</Text>
              </View>
            );
          }
          return (
            <View key={slot.startMinute} testID={`slot-${slot.startMinute}`} style={styles.tick}>
              <View style={styles.tickLine} />
            </View>
          );
        }
        const thumbnail = thumbnails[slot.startMinute];
        const selected = selectedMinute === slot.startMinute;
        return (
          <Pressable
            key={slot.startMinute}
            testID={`slot-${slot.startMinute}`}
            accessibilityRole="button"
            accessibilityLabel={`Clip at ${label}${slot.isFlagged ? ', flagged' : ''}`}
            onPress={() => onSlotPress?.(slot)}
            onLongPress={() => onSlotLongPress(slot)}
            style={styles.clip}
          >
            <View style={[styles.tile, selected ? styles.selected : null]}>
              {thumbnail ? (
                <Image
                  testID={`thumb-${slot.startMinute}`}
                  source={{ uri: thumbnail }}
                  style={styles.thumb}
                  accessibilityLabel={`Preview at ${label}`}
                />
              ) : (
                <PlayGlyph />
              )}
              {slot.isFlagged ? (
                <View testID={`flag-${slot.startMinute}`} style={styles.flag}>
                  <FlagGlyph />
                </View>
              ) : null}
            </View>
            <Text style={[styles.clipLabel, selected ? styles.clipLabelSelected : null]}>
              {label}
            </Text>
          </Pressable>
        );
      })}
    </ScrollView>
  );
}

function PlayGlyph() {
  return (
    <Svg width={20} height={20} viewBox="0 0 24 24">
      <Path d="M8 5.5v13l10.5-6.5z" fill={colors.textMuted} />
    </Svg>
  );
}

function FlagGlyph() {
  return (
    <Svg width={12} height={12} viewBox="0 0 24 24">
      <Path d="M5 21V4h11l-1.5 4L16 12H7v9z" fill={colors.background} />
    </Svg>
  );
}

const TILE_WIDTH = 64;

const styles = StyleSheet.create({
  strip: { flexGrow: 0 },
  content: { alignItems: 'flex-start', paddingRight: spacing.md },
  tick: { width: 6, height: 56, alignItems: 'center' },
  tickLine: {
    width: StyleSheet.hairlineWidth,
    height: 10,
    marginTop: 18,
    backgroundColor: colors.textMuted,
    opacity: 0.35,
  },
  hour: { height: 56, paddingRight: 2 },
  hourLine: {
    width: StyleSheet.hairlineWidth,
    height: 36,
    backgroundColor: colors.textMuted,
    opacity: 0.8,
  },
  hourLabel: { color: colors.textMuted, fontSize: 11, marginTop: 4 },
  clip: { marginHorizontal: spacing.xs, alignItems: 'center' },
  tile: {
    width: TILE_WIDTH,
    height: 36,
    borderRadius: 6,
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.accent,
    alignItems: 'center',
    justifyContent: 'center',
    overflow: 'hidden',
  },
  selected: { borderWidth: 2, borderColor: colors.text },
  thumb: { position: 'absolute', width: '100%', height: '100%' },
  flag: {
    position: 'absolute',
    top: 3,
    right: 3,
    backgroundColor: colors.warning,
    borderRadius: 3,
    padding: 1,
  },
  clipLabel: { color: colors.textMuted, fontSize: 11, marginTop: 4 },
  clipLabelSelected: { color: colors.text, fontWeight: '600' },
});
