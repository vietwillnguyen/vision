import type { SupabaseClient } from '@supabase/supabase-js';
import React, { useMemo, useState } from 'react';
import { Button, Share, StyleSheet, Text, View } from 'react-native';

import { ReelPlayer } from '../components/ReelPlayer';
import { useReel, useReelsInRange } from '../hooks/useReel';
import { useSignedUrl } from '../hooks/useSignedUrl';
import { buildHeatmapCells } from '../logic/heatmap';
import { utcDateString, utcRangeEndingAt } from '../logic/dates';
import { buildReelSignedUrlRequest } from '../logic/segmentExport';
import { ArchiveScreen } from '../screens/ArchiveScreen';
import { colors, spacing } from '../theme';

const HEATMAP_DAYS = 30;

interface ArchiveContainerProps {
  client: SupabaseClient;
  deviceId: string;
  now?: () => Date;
}

export function ArchiveContainer({ client, deviceId, now = () => new Date() }: ArchiveContainerProps) {
  // buildHeatmapCells assumes UTC-midnight-aligned bounds; normalize before calling it
  // or users west of UTC get off-by-one day cells.
  const { start, end } = useMemo(() => utcRangeEndingAt(now(), HEATMAP_DAYS), [now]);
  const reelsState = useReelsInRange(client, deviceId, utcDateString(start), utcDateString(end));
  const [selectedDate, setSelectedDate] = useState<string | null>(null);
  const selectedReel = useReel(client, deviceId, selectedDate);
  const signedUrl = useSignedUrl(
    client,
    selectedReel.kind === 'ready' && selectedReel.reel.s3Key ? buildReelSignedUrlRequest(selectedReel.reel.s3Key) : null,
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
    return (
      <View style={styles.playerWrap}>
        {selectedReel.kind === 'error' ? (
          <Text style={styles.error} accessibilityLabel="Reel error">
            {selectedReel.message}
          </Text>
        ) : (
          <>
            <ReelPlayer
              videoUri={signedUrl}
              title={selectedDate}
              onShare={() => {
                if (signedUrl) Share.share({ url: signedUrl, message: signedUrl });
              }}
            />
            {selectedReel.kind === 'none' ? (
              <Text style={styles.muted}>No reel for this day.</Text>
            ) : null}
          </>
        )}
        <View style={styles.backWrap}>
          <Button
            title="Back to archive"
            color={colors.textMuted}
            accessibilityLabel="Back to archive"
            onPress={() => setSelectedDate(null)}
          />
        </View>
      </View>
    );
  }

  const cells = buildHeatmapCells(reelsState.reels, start, end);
  return <ArchiveScreen cells={cells} onDayPress={setSelectedDate} />;
}

const styles = StyleSheet.create({
  message: { flex: 1, backgroundColor: colors.background, alignItems: 'center', justifyContent: 'center' },
  playerWrap: { flex: 1, backgroundColor: colors.background },
  backWrap: { padding: spacing.md },
  muted: { color: colors.textMuted, fontSize: 16, textAlign: 'center' },
  error: { color: colors.danger, fontSize: 16 },
});
