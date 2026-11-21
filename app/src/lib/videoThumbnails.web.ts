// expo-video-thumbnails has no web implementation. Metro resolves this file
// over videoThumbnails.ts on web, so the native module is never imported
// from a web bundle - the platform-extension convention, not a runtime
// require()/catch, is what keeps the web bundle from crashing at load time.
//
// On web a thumbnail is drawn by the browser itself: a muted, detached
// <video> seeks to the requested time and a <canvas> copies that frame into a
// JPEG data URI. Storage serves CORS headers, so with crossOrigin set the
// canvas stays readable.
import type { VideoThumbnailsOptions } from 'expo-video-thumbnails';

const THUMB_WIDTH = 160;
const TIMEOUT_MS = 15000;
// Seeking to exactly 0 does not reliably fire `seeked`, and the first frame of
// an H.264 segment is often still settling; half a second in is a real frame.
const MIN_SEEK_SEC = 0.5;

interface VideoLike {
  crossOrigin: string | null;
  muted: boolean;
  preload: string;
  playsInline: boolean;
  src: string;
  currentTime: number;
  duration: number;
  videoWidth: number;
  videoHeight: number;
  addEventListener(type: string, listener: () => void): void;
  removeAttribute(name: string): void;
  load(): void;
}

interface CanvasLike {
  width: number;
  height: number;
  getContext(type: '2d'): {
    drawImage(image: unknown, dx: number, dy: number, dw: number, dh: number): void;
  } | null;
  toDataURL(type: string, quality?: number): string;
}

export interface DocumentLike {
  createElement(tag: 'video'): unknown;
  createElement(tag: 'canvas'): unknown;
}

export function createThumbnailer(doc: DocumentLike) {
  return function getThumbnail(
    uri: string,
    options?: VideoThumbnailsOptions,
  ): Promise<{ uri: string; width: number; height: number }> {
    return new Promise((resolve, reject) => {
      const video = doc.createElement('video') as VideoLike;
      let settled = false;
      const finish = (
        error: Error | null,
        result?: { uri: string; width: number; height: number },
      ) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        // Drop the source so the browser stops buffering the segment.
        video.removeAttribute('src');
        video.load();
        if (error) reject(error);
        else if (result) resolve(result);
      };
      const timer = setTimeout(() => finish(new Error('thumbnail timed out')), TIMEOUT_MS);

      video.crossOrigin = 'anonymous';
      video.muted = true;
      video.preload = 'auto';
      video.playsInline = true;
      video.addEventListener('loadeddata', () => {
        const wanted = Math.max((options?.time ?? 0) / 1000, MIN_SEEK_SEC);
        const duration = Number.isFinite(video.duration) ? video.duration : wanted;
        video.currentTime = Math.min(wanted, duration / 2);
      });
      video.addEventListener('seeked', () => {
        try {
          const canvas = doc.createElement('canvas') as CanvasLike;
          const ratio = video.videoWidth > 0 ? video.videoHeight / video.videoWidth : 9 / 16;
          canvas.width = THUMB_WIDTH;
          canvas.height = Math.round(THUMB_WIDTH * ratio);
          const context = canvas.getContext('2d');
          if (!context) throw new Error('no 2d canvas context');
          context.drawImage(video, 0, 0, canvas.width, canvas.height);
          finish(null, {
            uri: canvas.toDataURL('image/jpeg', options?.quality ?? 0.7),
            width: canvas.width,
            height: canvas.height,
          });
        } catch (error) {
          finish(error instanceof Error ? error : new Error(String(error)));
        }
      });
      video.addEventListener('error', () => finish(new Error('video failed to load')));
      video.src = uri;
    });
  };
}

const webDocument: DocumentLike | undefined =
  typeof document === 'undefined' ? undefined : (document as unknown as DocumentLike);

export const isAvailable = webDocument !== undefined;

export async function getThumbnailAsync(
  uri: string,
  options?: VideoThumbnailsOptions,
): Promise<{ uri: string }> {
  if (!webDocument) {
    throw new Error('Video thumbnails need a browser document');
  }
  return createThumbnailer(webDocument)(uri, options);
}
