import { File, Paths } from 'expo-file-system';
import * as MediaLibrary from 'expo-media-library';

export const isAvailable = true;

export class PermissionDeniedError extends Error {}

export async function saveUrlToCameraRoll(url: string, filenamePrefix: string): Promise<void> {
  const { granted } = await MediaLibrary.requestPermissionsAsync();
  if (!granted) {
    throw new PermissionDeniedError('Photo library permission was not granted');
  }
  const file = await File.downloadFileAsync(url, new File(Paths.cache, `${filenamePrefix}-${Date.now()}.mp4`));
  await MediaLibrary.saveToLibraryAsync(file.uri);
}
