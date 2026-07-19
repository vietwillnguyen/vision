// expo-file-system's downloadFileAsync and expo-media-library have no web
// implementation. Metro resolves this file over mediaSave.ts on web, so
// neither native module is ever imported from a web bundle.
export const isAvailable = false;

export class PermissionDeniedError extends Error {}

export async function saveUrlToCameraRoll(_url: string, _filenamePrefix: string): Promise<void> {
  throw new Error('Saving to camera roll is not supported on web');
}
