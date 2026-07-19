import * as videoThumbnailsWeb from '../../src/lib/videoThumbnails.web';

describe('videoThumbnails.web', () => {
  it('reports unavailable and rejects getThumbnailAsync', async () => {
    expect(videoThumbnailsWeb.isAvailable).toBe(false);
    await expect(videoThumbnailsWeb.getThumbnailAsync('https://example.com/video.mp4')).rejects.toThrow(
      'Video thumbnails are not supported on web',
    );
  });
});
