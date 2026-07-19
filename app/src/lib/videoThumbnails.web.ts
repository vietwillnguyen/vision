// expo-video-thumbnails has no web implementation. Metro resolves this file
// over videoThumbnails.ts on web, so the native module is never imported
// from a web bundle - the platform-extension convention, not a runtime
// require()/catch, is what keeps the web bundle from crashing at load time.
import type { VideoThumbnailsOptions } from 'expo-video-thumbnails';

export const isAvailable = false;

export async function getThumbnailAsync(
  _uri: string,
  _options?: VideoThumbnailsOptions,
): Promise<{ uri: string }> {
  throw new Error('Video thumbnails are not supported on web');
}
