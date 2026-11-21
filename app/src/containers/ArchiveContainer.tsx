import React, { useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { ReelPlayer } from '../components/ReelPlayer';
import { useReel, useReelsInRange } from '../hooks/useReel';
import { useSignedUrlState } from '../hooks/useSignedUrl';
import { canShare, shareUrl } from '../lib/share';
import { buildHeatmapCells } from '../logic/heatmap';
import { utcDateString, utcRangeEndingAt } from '../logic/dates';
import { formatDayLabel } from '../logic/readOnly';
import { buildReelSignedUrlRequest } from '../logic/segmentExport';
import { ArchiveScreen } from '../screens/ArchiveScreen';
import { colors, spacing } from '../theme';
import type { VisionClient } from '../lib/supabase';

const HEATMAP_DAYS = 30;

interface ArchiveContainerProps {
  client: VisionClient;
  deviceId: string;
  now?: () => Date;
}

export function ArchiveContainer({
  client,
  deviceId,
  now = () => new Date(),
}: ArchiveContainerProps) {
  // buildHeatmapCells assumes UTC-midnight-aligned bounds; normalize before calling it
  // or users west of UTC get off-by-one day cells.
  const [{ start, end }] = useState(() => utcRangeEndingAt(now(), HEATMAP_DAYS));
  const reelsState = useReelsInRange(client, deviceId, utcDateString(start), utcDateString(end));
  const [selectedDate, setSelectedDate] = useState<string | null>(null);
  const selectedReel = useReel(client, deviceId, selectedDate);
  const video = useSignedUrlState(
    client,
    selectedReel.kind === 'ready' && selectedReel.reel.s3Key
      ? buildReelSignedUrlRequest(selectedReel.reel.s3Key)
      : null,
  );

  if (reelsState.kind === 'loading') {
    return (
      <View style={styles.message}>
        <Text style={styles.muted}>Loading archive...</Text>
      </View>
    );
  }
  if (reelsState.kind === 'error') {
    return (
      <View style={styles.message}>
        <Text style={styles.error} accessibilityLabel="Archive error">
          {reelsState.message}
        </Text>
      </View>
    );
  }

  if (selectedDate) {
    const videoUri = video.kind === 'ready' ? video.url : null;
    const videoError =
      selectedReel.kind === 'none'
        ? 'No reel was made for this day.'
        : selectedReel.kind === 'ready' && !selectedReel.reel.s3Key
          ? 'This reel has no video file yet.'
          : video.kind === 'error'
            ? `Couldn't load the reel video (${video.message}).`
            : null;
    return (
      <View style={styles.playerWrap}>
        <Pressable
          testID="back-to-archive"
          accessibilityRole="button"
          accessibilityLabel="Back to archive"
          onPress={() => setSelectedDate(null)}
          hitSlop={8}
          style={styles.back}
        >
          <Text style={styles.backText}>‹ Archive</Text>
        </Pressable>
        {selectedReel.kind === 'error' ? (
          <Text style={styles.error} accessibilityLabel="Reel error">
            {selectedReel.message}
          </Text>
        ) : (
          <ReelPlayer
            videoUri={videoUri}
            videoError={videoError}
            title={formatDayLabel(selectedDate)}
            onShare={canShare() ? () => videoUri && shareUrl(videoUri) : undefined}
          />
        )}
      </View>
    );
  }

  const cells = buildHeatmapCells(reelsState.reels, start, end);
  return <ArchiveScreen cells={cells} onDayPress={setSelectedDate} />;
}

const styles = StyleSheet.create({
  message: {
    flex: 1,
    backgroundColor: colors.background,
    alignItems: 'center',
    justifyContent: 'center',
  },
  playerWrap: { flex: 1, backgroundColor: colors.background },
  back: { alignSelf: 'flex-start', paddingHorizontal: spacing.md, paddingTop: spacing.md },
  backText: { color: colors.accent, fontSize: 16 },
  muted: { color: colors.textMuted, fontSize: 16, textAlign: 'center' },
  error: { color: colors.danger, fontSize: 16 },
});
