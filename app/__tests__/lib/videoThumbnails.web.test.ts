import * as videoThumbnailsWeb from '../../src/lib/videoThumbnails.web';

type Listener = () => void;

function fakeDocument({ fail = false }: { fail?: boolean } = {}) {
  const listeners: Record<string, Listener> = {};
  const drawn: unknown[] = [];
  const video = {
    crossOrigin: null as string | null,
    muted: false,
    preload: '',
    playsInline: false,
    duration: 60,
    videoWidth: 1280,
    videoHeight: 720,
    removed: false,
    _src: '',
    _time: 0,
    get src() {
      return this._src;
    },
    set src(value: string) {
      this._src = value;
      // The browser loads asynchronously, then seeks when asked.
      Promise.resolve().then(() => (fail ? listeners.error() : listeners.loadeddata()));
    },
    get currentTime() {
      return this._time;
    },
    set currentTime(value: number) {
      this._time = value;
      Promise.resolve().then(() => listeners.seeked());
    },
    addEventListener: (type: string, listener: Listener) => {
      listeners[type] = listener;
    },
    removeAttribute() {
      video.removed = true;
    },
    load: () => {},
  };
  const canvas = {
    width: 0,
    height: 0,
    getContext: () => ({ drawImage: (image: unknown) => drawn.push(image) }),
    toDataURL: (type: string) => `data:${type};base64,AAAA`,
  };
  const doc = {
    createElement: (tag: string) => (tag === 'video' ? video : canvas),
  } as unknown as videoThumbnailsWeb.DocumentLike;
  return { doc, video, canvas, drawn };
}

describe('videoThumbnails.web', () => {
  it('is unavailable outside a browser document', async () => {
    expect(videoThumbnailsWeb.isAvailable).toBe(false);
    await expect(
      videoThumbnailsWeb.getThumbnailAsync('https://example.com/video.mp4'),
    ).rejects.toThrow('Video thumbnails need a browser document');
  });

  it('draws a seeked frame into a small CORS-safe JPEG', async () => {
    const { doc, video, canvas, drawn } = fakeDocument();

    const result = await videoThumbnailsWeb.createThumbnailer(doc)('https://signed/s1.mp4', {
      time: 0,
    });

    expect(video.crossOrigin).toBe('anonymous');
    expect(video.muted).toBe(true);
    expect(video.currentTime).toBe(0.5);
    expect(drawn).toEqual([video]);
    expect([canvas.width, canvas.height]).toEqual([160, 90]);
    expect(result.uri).toBe('data:image/jpeg;base64,AAAA');
    expect(video.removed).toBe(true);
  });

  it('rejects when the video cannot load', async () => {
    const { doc } = fakeDocument({ fail: true });

    await expect(
      videoThumbnailsWeb.createThumbnailer(doc)('https://signed/missing.mp4'),
    ).rejects.toThrow('video failed to load');
  });
});
