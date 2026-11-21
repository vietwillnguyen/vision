import React, { useMemo, useState } from 'react';
import { Alert, Platform, ScrollView, StyleSheet, Text, View } from 'react-native';

import { SegmentPreview } from '../components/SegmentPreview';
import { TimelineScrubber } from '../components/TimelineScrubber';
import { useSegments } from '../hooks/useSegments';
import { useSignedUrlState } from '../hooks/useSignedUrl';
import { useSlotThumbnails } from '../hooks/useSlotThumbnails';
import * as mediaSave from '../lib/mediaSave';
import { canShare, shareUrl } from '../lib/share';
import { toUtcMidnight, utcDateString } from '../logic/dates';
import { formatDayLabel } from '../logic/readOnly';
import { buildSegmentSignedUrlRequest } from '../logic/segmentExport';
import { buildTimelineSlots, trimToRecordedHours, type TimelineSlot } from '../logic/timeline';
import { colors, spacing } from '../theme';
import type { Segment } from '../types';
import type { VisionClient } from '../lib/supabase';

interface RawFootageContainerProps {
  client: VisionClient;
  deviceId: string;
  now?: () => Date;
  /** The read-only demo: no clip preferences, and the timeline says why. */
  readOnly?: boolean;
}

// One-minute slots: the pendant records one-minute segments, so a coarser
// slot would hide all but one segment of each run.
const SLOT_MINUTES = 1;

export function RawFootageContainer({
  client,
  deviceId,
  now = () => new Date(),
  readOnly = false,
}: RawFootageContainerProps) {
  const [dayStart] = useState(() => toUtcMidnight(now()));
  const { state, setUserFeedback } = useSegments(client, deviceId, dayStart.toISOString());
  const [selected, setSelected] = useState<Segment | null>(null);

  const slots = useMemo(
    () =>
      state.kind === 'ready'
        ? trimToRecordedHours(
            buildTimelineSlots(state.segments, dayStart, SLOT_MINUTES),
            SLOT_MINUTES,
          )
        : [],
    [state, dayStart],
  );
  const thumbnails = useSlotThumbnails(client, slots);
  const preview = useSignedUrlState(
    client,
    selected && selected.s3Key ? buildSegmentSignedUrlRequest(selected.s3Key) : null,
  );
  const previewUrl = preview.kind === 'ready' ? preview.url : null;

  const onSlotPress = (slot: TimelineSlot) => {
    if (slot.segment) setSelected(slot.segment);
  };

  const onSlotLongPress = (slot: TimelineSlot) => {
    const segment = slot.segment;
    if (!segment) return;
    // Alert is a no-op on web, and a read-only account cannot store a
    // preference, so both just open the clip.
    if (readOnly || Platform.OS === 'web') {
      setSelected(segment);
      return;
    }
    Alert.alert('Segment options', segment.recordedAt, [
      { text: 'Preview', onPress: () => setSelected(segment) },
      { text: 'Always include', onPress: () => void setUserFeedback(segment.id, 'include') },
      { text: 'Never include', onPress: () => void setUserFeedback(segment.id, 'exclude') },
      { text: 'Clear preference', onPress: () => void setUserFeedback(segment.id, null) },
      { text: 'Cancel', style: 'cancel' },
    ]);
  };

  const onSave = async () => {
    if (!selected || !previewUrl) return;
    if (!mediaSave.isAvailable) {
      Alert.alert('Not available', 'Saving to camera roll is not supported on web.');
      return;
    }
    try {
      await mediaSave.saveUrlToCameraRoll(previewUrl, selected.id);
      Alert.alert('Saved', 'Segment saved to your camera roll.');
    } catch (error) {
      if (error instanceof mediaSave.PermissionDeniedError) {
        Alert.alert('Permission needed', 'Allow photo library access to save segments.');
        return;
      }
      Alert.alert('Save failed', String(error));
    }
  };

  if (state.kind === 'loading') {
    return (
      <View style={styles.message}>
        <Text style={styles.muted}>Loading footage...</Text>
      </View>
    );
  }
  if (state.kind === 'error') {
    return (
      <View style={styles.message}>
        <Text style={styles.error} accessibilityLabel="Footage error">
          {state.message}
        </Text>
      </View>
    );
  }

  return (
    <ScrollView style={styles.container} contentContainerStyle={styles.content}>
      <Text style={styles.title} accessibilityRole="header">
        Raw Footage
      </Text>
      <Text style={styles.subtitle}>{formatDayLabel(utcDateString(dayStart))}</Text>
      {slots.length === 0 ? (
        <Text style={styles.empty}>No footage was recorded on this day.</Text>
      ) : (
        <>
          <TimelineScrubber
            slots={slots}
            thumbnails={thumbnails}
            onSlotPress={onSlotPress}
            onSlotLongPress={onSlotLongPress}
            selectedMinute={selectedMinute(slots, selected)}
          />
          <Text style={styles.hint}>
            {readOnly || Platform.OS === 'web'
              ? 'Tap a clip to play it. Times are UTC.'
              : 'Tap a clip to play it, or long-press for reel preferences. Times are UTC.'}
          </Text>
          {readOnly ? (
            <Text testID="read-only-note" style={styles.readOnlyNote}>
              This demo is read-only, so the reel preferences (Always include, Never include) are
              turned off.
            </Text>
          ) : null}
        </>
      )}
      <View style={styles.previewWrap}>
        <SegmentPreview
          segment={selected}
          videoUri={previewUrl}
          videoError={
            preview.kind === 'error' ? `Couldn't load this clip (${preview.message}).` : null
          }
          onSave={mediaSave.isAvailable ? () => void onSave() : undefined}
          onShare={canShare() ? () => previewUrl && shareUrl(previewUrl) : undefined}
          onClose={() => setSelected(null)}
        />
      </View>
    </ScrollView>
  );
}

function selectedMinute(slots: TimelineSlot[], selected: Segment | null): number | null {
  if (!selected) return null;
  return slots.find((slot) => slot.segment?.id === selected.id)?.startMinute ?? null;
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.background },
  content: { padding: spacing.md },
  title: { color: colors.text, fontSize: 20, fontWeight: '600' },
  subtitle: { color: colors.textMuted, fontSize: 14, marginTop: 2, marginBottom: spacing.md },
  empty: { color: colors.textMuted, fontSize: 16, marginTop: spacing.lg },
  hint: { color: colors.textMuted, fontSize: 13, marginTop: spacing.sm },
  readOnlyNote: { color: colors.warning, fontSize: 13, marginTop: spacing.xs },
  previewWrap: { marginTop: spacing.md },
  message: {
    flex: 1,
    backgroundColor: colors.background,
    alignItems: 'center',
    justifyContent: 'center',
  },
  muted: { color: colors.textMuted, fontSize: 16 },
  error: { color: colors.danger, fontSize: 16 },
});
