import React from 'react';
import { Platform, StyleSheet, useWindowDimensions, View } from 'react-native';

import { colors } from '../theme';

/** Widths at or above this get the phone frame; below it the app fills the page. */
export const FRAME_BREAKPOINT = 600;
const FRAME_WIDTH = 400;
const FRAME_MAX_HEIGHT = 860;
const FRAME_MARGIN = 24;

/**
 * Presents the phone app sensibly in a desktop browser.
 *
 * The app is designed for a phone, so on a wide web viewport it sits in a
 * centred phone-width frame instead of stretching across the screen. On a
 * narrow viewport (a phone's browser) and on native it is a pass-through, so
 * native layouts are untouched.
 */
export function WebFrame({
  children,
  platform = Platform.OS,
}: {
  children: React.ReactNode;
  platform?: typeof Platform.OS;
}) {
  const { width, height } = useWindowDimensions();
  if (platform !== 'web') {
    return <>{children}</>;
  }
  const framed = width >= FRAME_BREAKPOINT;
  return (
    <View style={[styles.page, framed ? styles.pageFramed : null]}>
      <View
        testID="web-frame"
        style={
          framed
            ? [styles.phone, { height: Math.min(height - 2 * FRAME_MARGIN, FRAME_MAX_HEIGHT) }]
            : styles.fill
        }
      >
        {children}
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.background },
  pageFramed: {
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: '#07080B',
  },
  fill: { flex: 1 },
  phone: {
    width: FRAME_WIDTH,
    overflow: 'hidden',
    borderRadius: 28,
    borderWidth: 1,
    borderColor: colors.surface,
    backgroundColor: colors.background,
    // Ignored on native, which never reaches this branch.
    boxShadow: '0 24px 64px rgba(0, 0, 0, 0.55)',
  },
});
