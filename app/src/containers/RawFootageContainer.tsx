import type { SupabaseClient } from '@supabase/supabase-js';
import React, { useMemo, useState } from 'react';
import { Alert, Share, StyleSheet, Text, View } from 'react-native';

import { SegmentPreview } from '../components/SegmentPreview';
import { TimelineScrubber } from '../components/TimelineScrubber';
import { useSegments } from '../hooks/useSegments';
import { useSignedUrl } from '../hooks/useSignedUrl';
import { useSlotThumbnails } from '../hooks/useSlotThumbnails';
import * as mediaSave from '../lib/mediaSave';
import { toUtcMidnight } from '../logic/dates';
import { buildSegmentSignedUrlRequest } from '../logic/segmentExport';
import { buildTimelineSlots, type TimelineSlot } from '../logic/timeline';
import { colors, spacing } from '../theme';
import type { Segment } from '../types';

interface RawFootageContainerProps {
  client: SupabaseClient;
  deviceId: string;
  now?: () => Date;
}

export function RawFootageContainer({ client, deviceId, now = () => new Date() }: RawFootageContainerProps) {
  const [dayStart] = useState(() => toUtcMidnight(now()));
  const { state, setUserFeedback } = useSegments(client, deviceId, dayStart.toISOString());
  const [selected, setSelected] = useState<Segment | null>(null);

  const slots = useMemo(
    () => (state.kind === 'ready' ? buildTimelineSlots(state.segments, dayStart) : []),
    [state, dayStart],
  );
  const thumbnails = useSlotThumbnails(client, slots);
  const previewUrl = useSignedUrl(
    client,
    selected && selected.s3Key ? buildSegmentSignedUrlRequest(selected.s3Key) : null,
  );

  const onSlotLongPress = (slot: TimelineSlot) => {
    const segment = slot.segment;
    if (!segment) return;
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
    <View style={styles.container}>
      <Text style={styles.title} accessibilityRole="header">
        Raw Footage
      </Text>
      <TimelineScrubber slots={slots} thumbnails={thumbnails} onSlotLongPress={onSlotLongPress} />
      <View style={styles.previewWrap}>
        <SegmentPreview
          segment={selected}
          videoUri={previewUrl}
          onSave={() => void onSave()}
          onShare={() => {
            if (previewUrl) Share.share({ url: previewUrl, message: previewUrl });
          }}
          onClose={() => setSelected(null)}
        />
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.background, padding: spacing.md },
  title: { color: colors.text, fontSize: 20, fontWeight: '600', marginBottom: spacing.md },
  previewWrap: { marginTop: spacing.md },
  message: { flex: 1, backgroundColor: colors.background, alignItems: 'center', justifyContent: 'center' },
  muted: { color: colors.textMuted, fontSize: 16 },
  error: { color: colors.danger, fontSize: 16 },
});
