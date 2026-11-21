import React from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { colors, spacing } from '../theme';

interface DemoBannerProps {
  onExit: () => void;
}

/**
 * The persistent strip that says this is the read-only demo, on every tab.
 */
export function DemoBanner({ onExit }: DemoBannerProps) {
  return (
    <View
      testID="demo-banner"
      style={styles.banner}
      accessibilityRole="summary"
      accessibilityLabel="Demo mode. Sample footage from the bench prototype. Read-only."
    >
      <View style={styles.pill}>
        <Text style={styles.pillText}>DEMO</Text>
      </View>
      <View style={styles.copy}>
        <Text style={styles.title} numberOfLines={1}>
          Demo - sample footage
        </Text>
        <Text style={styles.detail} numberOfLines={1}>
          Bench prototype · read-only
        </Text>
      </View>
      <Pressable
        testID="exit-demo-button"
        accessibilityRole="button"
        accessibilityLabel="Exit demo"
        onPress={onExit}
        hitSlop={8}
        style={({ pressed }) => [styles.exit, pressed ? styles.exitPressed : null]}
      >
        <Text style={styles.exitText}>Exit</Text>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  banner: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: colors.surface,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: colors.warning,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
  },
  pill: {
    backgroundColor: colors.warning,
    borderRadius: 4,
    paddingHorizontal: 6,
    paddingVertical: 2,
    marginRight: spacing.sm,
  },
  pillText: { color: colors.background, fontSize: 11, fontWeight: '700', letterSpacing: 0.5 },
  copy: { flex: 1 },
  title: { color: colors.text, fontSize: 14, fontWeight: '600' },
  detail: { color: colors.textMuted, fontSize: 12, marginTop: 1 },
  exit: {
    borderWidth: 1,
    borderColor: colors.textMuted,
    borderRadius: 6,
    paddingHorizontal: spacing.sm,
    paddingVertical: 4,
    marginLeft: spacing.sm,
  },
  exitPressed: { backgroundColor: colors.background },
  exitText: { color: colors.text, fontSize: 13 },
});
