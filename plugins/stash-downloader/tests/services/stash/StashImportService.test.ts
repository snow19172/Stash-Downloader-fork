import { afterEach, describe, expect, it, vi } from 'vitest';
import { getDownloadService } from '@/services/download';
import { getBrowserDownloadService } from '@/services/download/BrowserDownloadService';
import { getStashService } from '@/services/stash/StashGraphQLService';
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

  it('downloads, scans, and creates a Stash gallery from gallery metadata', async () => {
    const downloadService = getDownloadService();
    const stashService = getStashService();
    const download = vi.spyOn(downloadService, 'downloadServerSide')
      .mockResolvedValueOnce({ success: true, file_path: '/stash/images/photo-1.jpg' })
      .mockResolvedValueOnce({ success: true, file_path: '/stash/images/photo-2.jpg' });
    const scan = vi.spyOn(stashService, 'triggerScan').mockResolvedValue('scan-job');
    vi.spyOn(stashService, 'getImageLibraryPath').mockResolvedValue('/stash/images');
    vi.spyOn(stashService, 'waitForJob').mockResolvedValue({ success: true });
    vi.spyOn(stashService, 'findImageByPath')
      .mockResolvedValueOnce({ id: 'image-1' })
      .mockResolvedValueOnce({ id: 'image-2' });
    const updateImage = vi.spyOn(stashService, 'updateImage').mockResolvedValue();
    vi.spyOn(stashService, 'getOrCreatePerformer').mockResolvedValue({
      id: 'performer-1',
      name: 'creator_handle',
    });
    vi.spyOn(stashService, 'getOrCreateStudio').mockResolvedValue({
      id: 'studio-1',
      name: 'creator_handle',
    });
    const createGallery = vi.spyOn(stashService, 'createGallery').mockResolvedValue({
      id: 'gallery-1',
      title: 'TikTok photo post',
      organized: false,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
      images: [],
    });
    const addGalleryImages = vi.spyOn(stashService, 'addGalleryImages').mockResolvedValue();
    const setGalleryCover = vi.spyOn(stashService, 'setGalleryCover').mockResolvedValue();
    const item: IDownloadItem = {
      id: 'gallery-item',
      url: 'https://www.tiktok.com/@creator/photo/123456789',
      status: DownloadStatus.Pending,
      addedAt: new Date(),
      metadata: {
        url: 'https://www.tiktok.com/@creator/photo/123456789',
        title: 'TikTok photo post',
        contentType: ContentType.Gallery,
        date: '2025-03-04',
        studio: 'creator_handle',
        performers: ['creator_handle'],
        galleryImages: [
          { url: 'https://cdn.example/photo-1', filename: 'photo-1.jpg' },
          { url: 'https://cdn.example/photo-2', filename: 'photo-2.jpg' },
        ],
      },
      editedMetadata: {
        title: 'TikTok photo post',
      },
    };

    const gallery = await new StashImportService().importToStash(item);

    expect(gallery.id).toBe('gallery-1');
    expect(download).toHaveBeenCalledTimes(2);
    expect(download.mock.calls[0]?.[1]).toEqual(expect.objectContaining({
      outputDir: '/stash/images',
      forceOutputDir: true,
      forceDirect: true,
      filename: 'photo-1.jpg',
    }));
    expect(scan).toHaveBeenCalledWith(['/stash/images/photo-1.jpg', '/stash/images/photo-2.jpg']);
    expect(updateImage).toHaveBeenNthCalledWith(1, 'image-1', {
      title: '123456789-01',
      url: 'https://www.tiktok.com/@creator/photo/123456789?image_index=1',
      date: '2025-03-04',
      performer_ids: ['performer-1'],
      studio_id: 'studio-1',
    });
    expect(updateImage).toHaveBeenNthCalledWith(2, 'image-2', {
      title: '123456789-02',
      url: 'https://www.tiktok.com/@creator/photo/123456789?image_index=2',
      date: '2025-03-04',
      performer_ids: ['performer-1'],
      studio_id: 'studio-1',
    });
    expect(createGallery).toHaveBeenCalledWith(expect.objectContaining({
      title: 'TikTok photo post',
      date: '2025-03-04',
      performer_ids: ['performer-1'],
      studio_id: 'studio-1',
    }));
    expect(addGalleryImages).toHaveBeenCalledWith('gallery-1', ['image-1', 'image-2']);
    expect(setGalleryCover).toHaveBeenCalledWith('gallery-1', 'image-1');
  });

  it('sets TikTok photo metadata and creator studio on a standalone image', async () => {
    const downloadService = getDownloadService();
    const stashService = getStashService();
    vi.spyOn(downloadService, 'download').mockResolvedValue(new Blob(['image']));
    vi.spyOn(stashService, 'getOrCreatePerformer').mockResolvedValue({
      id: 'performer-1',
      name: 'creator_handle',
    });
    vi.spyOn(stashService, 'getOrCreateStudio').mockResolvedValue({
      id: 'studio-1',
      name: 'creator_handle',
    });
    vi.spyOn(stashService, 'createImage').mockResolvedValue({
      id: 'image-1',
      title: 'TikTok photo post',
      organized: false,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
      files: [],
      paths: {},
    });
    const updateImage = vi.spyOn(stashService, 'updateImage').mockResolvedValue();
    vi.spyOn(getBrowserDownloadService(), 'downloadWithMetadata').mockResolvedValue();
    const item: IDownloadItem = {
      id: 'image-item',
      url: 'https://www.tiktok.com/@creator/photo/123456789?image_index=2',
      status: DownloadStatus.Pending,
      addedAt: new Date(),
      metadata: {
        url: 'https://www.tiktok.com/@creator/photo/123456789',
        title: 'TikTok photo post',
        date: '2025-03-04',
        studio: 'creator_handle',
        performers: ['creator_handle'],
        contentType: ContentType.Image,
        imageUrl: 'https://cdn.example/photo.jpg',
      },
      editedMetadata: {
        title: 'TikTok photo post',
      },
    };

    await new StashImportService().importToStash(item);

    expect(stashService.createImage).toHaveBeenCalledWith(expect.objectContaining({
      performer_ids: ['performer-1'],
      studio_id: 'studio-1',
    }));
    expect(updateImage).toHaveBeenCalledWith('image-1', {
      title: '123456789-02',
      url: 'https://www.tiktok.com/@creator/photo/123456789?image_index=2',
      date: '2025-03-04',
      performer_ids: ['performer-1'],
      studio_id: 'studio-1',
    });
  });
});
