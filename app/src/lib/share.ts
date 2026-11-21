import { Platform, Share } from 'react-native';

/**
 * Whether this platform has a share sheet. react-native-web maps Share to the
 * Web Share API, which most desktop browsers lack, so the Share button is
 * hidden there instead of rejecting on press.
 */
export function canShare(): boolean {
  if (Platform.OS !== 'web') return true;
  return typeof navigator !== 'undefined' && typeof navigator.share === 'function';
}

export function shareUrl(url: string): void {
  Share.share({ url, message: url }).catch((error: unknown) => {
    // Dismissing the sheet is not an error on native; on web it rejects.
    console.warn('share failed', error);
  });
}
