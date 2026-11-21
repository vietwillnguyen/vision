import { useVideoPlayer, VideoView } from 'expo-video';
import React from 'react';
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from 'react-native';

import type { Segment } from '../types';
import { usePauseOnBlur } from '../hooks/usePauseOnBlur';
import { colors, spacing } from '../theme';

interface SegmentPreviewProps {
  segment: Segment | null;
  videoUri: string | null;
  videoError?: string | null;
  /** Each action is omitted where the platform cannot do it. */
  onSave?: () => void;
  onShare?: () => void;
  onClose: () => void;
}

export function SegmentPreview({
  segment,
  videoUri,
  videoError,
  onSave,
  onShare,
  onClose,
}: SegmentPreviewProps) {
  // Must run unconditionally before any early return (rules of hooks).
  const player = useVideoPlayer(videoUri);
  usePauseOnBlur(player);

  if (!segment) {
    return null;
  }

  return (
    <View testID="segment-preview" style={styles.container}>
      <View style={styles.header}>
        <Text style={styles.timestamp}>{describeSegment(segment)}</Text>
        {segment.manuallyFlagged ? <Text style={styles.flagged}>Flagged</Text> : null}
      </View>
      {videoUri ? (
        <VideoView
          player={player}
          style={styles.video}
          nativeControls
          contentFit="contain"
          accessibilityLabel="Segment video player"
        />
      ) : (
        <View style={[styles.video, styles.placeholder]}>
          {videoError ? (
            <Text style={styles.error}>{videoError}</Text>
          ) : (
            <>
              <ActivityIndicator color={colors.textMuted} />
              <Text style={styles.muted}>Preparing playback...</Text>
            </>
          )}
        </View>
      )}
      <View style={styles.actions}>
        {onSave ? (
          <Action
            testID="save-button"
            label="Save"
            accessibilityLabel="Save segment to camera roll"
            onPress={onSave}
          />
        ) : null}
        {onShare ? (
          <Action
            testID="share-button"
            label="Share"
            accessibilityLabel="Share segment"
            onPress={onShare}
          />
        ) : null}
        <Action
          testID="close-button"
          label="Close"
          accessibilityLabel="Close preview"
          onPress={onClose}
          muted
        />
      </View>
    </View>
  );
}

function Action({
  testID,
  label,
  accessibilityLabel,
  onPress,
  muted = false,
}: {
  testID: string;
  label: string;
  accessibilityLabel: string;
  onPress: () => void;
  muted?: boolean;
}) {
  return (
    <Pressable
      testID={testID}
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel}
      onPress={onPress}
      style={({ pressed }) => [styles.action, pressed ? styles.actionPressed : null]}
    >
      <Text style={[styles.actionText, muted ? styles.actionMuted : null]}>{label}</Text>
    </Pressable>
  );
}

/** "09:05:00 UTC · 1 min" - the clip's start and length. */
export function describeSegment(segment: Segment): string {
  const time = new Date(segment.recordedAt).toISOString().slice(11, 19);
  const length =
    segment.durationSec >= 60 && segment.durationSec % 60 === 0
      ? `${segment.durationSec / 60} min`
      : `${segment.durationSec} s`;
  return `${time} UTC · ${length}`;
}

const styles = StyleSheet.create({
  container: { backgroundColor: colors.surface, borderRadius: 12, padding: spacing.md },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    marginBottom: spacing.sm,
  },
  timestamp: { color: colors.text, fontSize: 14 },
  flagged: { color: colors.warning, fontSize: 13, fontWeight: '600' },
  video: {
    width: '100%',
    aspectRatio: 16 / 9,
    borderRadius: 8,
    overflow: 'hidden',
    backgroundColor: colors.background,
  },
  placeholder: { alignItems: 'center', justifyContent: 'center', padding: spacing.md },
  muted: { color: colors.textMuted, marginTop: spacing.sm },
  error: { color: colors.danger, textAlign: 'center' },
  actions: { flexDirection: 'row', justifyContent: 'flex-end', marginTop: spacing.sm },
  action: {
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
    borderRadius: 6,
    marginLeft: spacing.xs,
  },
  actionPressed: { backgroundColor: colors.background },
  actionText: { color: colors.accent, fontSize: 15, fontWeight: '600' },
  actionMuted: { color: colors.textMuted },
});
