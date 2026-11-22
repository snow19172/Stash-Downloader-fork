import { afterEach, describe, expect, it, vi } from 'vitest';
import { getDownloadService } from '@/services/download';
import { StashImportService } from '@/services/stash/StashImportService';
import { ContentType, DownloadStatus, type IDownloadItem } from '@/types';

describe('StashImportService', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('passes the scraped image URL and content type to the downloader', async () => {
    const downloadService = getDownloadService();
    const download = vi.spyOn(downloadService, 'download')
      .mockRejectedValue(new Error('Stop after verifying download arguments'));
    const item: IDownloadItem = {
      id: 'image-item',
      url: 'https://booru.example/posts/123',
      status: DownloadStatus.Pending,
      addedAt: new Date(),
      metadata: {
        url: 'https://booru.example/posts/123',
        title: 'Booru image',
        contentType: ContentType.Image,
        imageUrl: 'https://cdn.example/images/image.jpg',
      },
      editedMetadata: {
        title: 'Booru image',
      },
    };

    await expect(new StashImportService().importToStash(item))
      .rejects.toThrow('Stop after verifying download arguments');

    expect(download).toHaveBeenCalledOnce();
    expect(download.mock.calls[0]).toEqual([
      item.url,
      expect.objectContaining({ filename: 'Booru image' }),
      undefined,
      item.metadata?.imageUrl,
      ContentType.Image,
    ]);
  });

  it('uses the TikTok page URL instead of its extracted CDN URL for imports', async () => {
    const downloadService = getDownloadService();
    const download = vi.spyOn(downloadService, 'download')
      .mockRejectedValue(new Error('Stop after verifying download arguments'));
    const item: IDownloadItem = {
      id: 'tiktok-item',
      url: 'https://www.tiktok.com/@creator/video/123456789',
      status: DownloadStatus.Pending,
      addedAt: new Date(),
      metadata: {
        url: 'https://www.tiktok.com/@creator/video/123456789',
        title: 'TikTok video',
        contentType: ContentType.Video,
        videoUrl: 'https://v16-webapp.tiktokcdn.com/video.mp4?token=temporary',
      },
      editedMetadata: {
        title: 'TikTok video',
      },
    };

    await expect(new StashImportService().importToStash(item))
      .rejects.toThrow('Stop after verifying download arguments');

    expect(download).toHaveBeenCalledOnce();
    expect(download.mock.calls[0]?.[0]).toBe(item.url);
    expect(download.mock.calls[0]?.[2]).toBeUndefined();
  });
});
