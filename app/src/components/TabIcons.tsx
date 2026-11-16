import React from 'react';
import Svg, { Circle, Path, Rect } from 'react-native-svg';

/**
 * Bottom-tab icons drawn as SVG.
 *
 * Without a `tabBarIcon`, bottom-tabs renders a text glyph (U+23F7) that web
 * system fonts lack, so every tab showed an empty box. SVG needs no icon font,
 * so nothing has to be preloaded with expo-font and the icons render the same
 * on web, iOS, and Android from the first frame.
 */
export type TabIconProps = { color: string; size: number };

type IconName = 'today' | 'raw' | 'device' | 'archive';

const STROKE = 2;

function Frame({
  name,
  color,
  size,
  children,
}: TabIconProps & { name: IconName; children: React.ReactNode }) {
  return (
    <Svg
      testID={`tab-icon-${name}`}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke={color}
      strokeWidth={STROKE}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {children}
    </Svg>
  );
}

/** Today's Reel: a play button in a circle. */
export function TodayReelIcon(props: TabIconProps) {
  return (
    <Frame name="today" {...props}>
      <Circle cx="12" cy="12" r="9" />
      <Path d="M10 8.5v7l5.5-3.5z" fill={props.color} />
    </Frame>
  );
}

/** Raw Footage: a video camera. */
export function RawFootageIcon(props: TabIconProps) {
  return (
    <Frame name="raw" {...props}>
      <Rect x="2.5" y="6.5" width="13" height="11" rx="2" />
      <Path d="M15.5 10.5l6-3.5v10l-6-3.5" />
    </Frame>
  );
}

/** Device: the wearable pendant - a rounded body with a lens. */
export function DeviceIcon(props: TabIconProps) {
  return (
    <Frame name="device" {...props}>
      <Rect x="6" y="3" width="12" height="18" rx="3" />
      <Circle cx="12" cy="10" r="2.5" />
      <Path d="M10 17h4" />
    </Frame>
  );
}

/** Archive: a calendar, since the archive is browsed by day. */
export function ArchiveIcon(props: TabIconProps) {
  return (
    <Frame name="archive" {...props}>
      <Rect x="3" y="5" width="18" height="16" rx="2" />
      <Path d="M3 10h18M8 3v4M16 3v4" />
    </Frame>
  );
}
