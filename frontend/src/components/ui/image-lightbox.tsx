import { DialogContent as DialogPrimitiveContent } from '@radix-ui/react-dialog';
import { ChevronLeftIcon, ChevronRightIcon, DownloadIcon, XIcon } from 'lucide-react';
import * as React from 'react';
import { useEffect, useState } from 'react';

import { Dialog, DialogOverlay, DialogPortal, DialogTitle } from '@/components/ui/dialog';
import { cn } from '@/lib/utils';

export interface LightboxImage {
  /** Stable identity for the image, used as a React key. */
  key: string;
  url: string;
  name: string;
}

export interface ImageLightboxProps {
  images: LightboxImage[];
  /** Index of the image to show; `null` keeps the lightbox closed. */
  index: number | null;
  onIndexChange: (index: number) => void;
  onClose: () => void;
}

const overlayButtonClass =
  'flex size-10 items-center justify-center rounded-full bg-black/60 text-white transition-colors hover:bg-black/80 focus:outline-none focus-visible:ring-2 focus-visible:ring-white';

/**
 * Full-screen viewer for chat image attachments.
 *
 * Radix supplies the focus trap, the Escape handler and the overlay click, so
 * this only adds what a lightbox needs on top: stepping between the images of
 * the same message with the arrow keys, and a zoom toggle that swaps
 * fit-to-viewport for the image's natural size inside a scrollable frame.
 */
export const ImageLightbox: React.FC<ImageLightboxProps> = ({
  images,
  index,
  onIndexChange,
  onClose,
}) => {
  const [zoomed, setZoomed] = useState(false);

  const isOpen = index !== null && index >= 0 && index < images.length;
  const image = isOpen ? images[index] : undefined;

  // A different image starts fit-to-viewport again, so stepping through a
  // message's images doesn't leave the next one scrolled off-screen.
  useEffect(() => {
    setZoomed(false);
  }, [image?.key]);

  if (!isOpen || !image) {
    return null;
  }

  const step = (delta: number) => {
    onIndexChange((index + delta + images.length) % images.length);
  };

  const handleKeyDown = (event: React.KeyboardEvent) => {
    if (images.length < 2) {
      return;
    }
    if (event.key === 'ArrowRight') {
      event.preventDefault();
      step(1);
    } else if (event.key === 'ArrowLeft') {
      event.preventDefault();
      step(-1);
    }
  };

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) {
          onClose();
        }
      }}
    >
      <DialogPortal>
        <DialogOverlay className="bg-black/90" />
        <DialogPrimitiveContent
          className="fixed inset-0 z-50 flex flex-col focus:outline-none"
          onKeyDown={handleKeyDown}
          aria-describedby={undefined}
          data-testid="image-lightbox"
        >
          <DialogTitle className="sr-only">{image.name}</DialogTitle>

          <div className="flex items-center justify-end gap-2 p-3">
            <a
              href={image.url}
              download={image.name}
              target="_blank"
              rel="noopener noreferrer"
              className={overlayButtonClass}
              aria-label={`Download ${image.name}`}
              data-testid="image-lightbox-download"
            >
              <DownloadIcon size={18} />
            </a>
            <button
              type="button"
              className={overlayButtonClass}
              onClick={onClose}
              aria-label="Close image viewer"
              data-testid="image-lightbox-close"
            >
              <XIcon size={18} />
            </button>
          </div>

          <div className="relative flex min-h-0 flex-1 items-center justify-center">
            {images.length > 1 && (
              <button
                type="button"
                className={cn(overlayButtonClass, 'absolute left-3 z-10')}
                onClick={() => step(-1)}
                aria-label="Previous image"
                data-testid="image-lightbox-previous"
              >
                <ChevronLeftIcon size={20} />
              </button>
            )}

            <div
              className={cn(
                'flex h-full w-full items-center justify-center px-4',
                zoomed && 'overflow-auto'
              )}
            >
              <button
                type="button"
                className={cn('flex max-h-full', zoomed ? 'cursor-zoom-out' : 'cursor-zoom-in')}
                onClick={() => setZoomed((current) => !current)}
                aria-label={zoomed ? 'Fit image to screen' : 'Zoom image to full size'}
                data-testid="image-lightbox-zoom"
              >
                <img
                  src={image.url}
                  alt={image.name}
                  className={cn(
                    'rounded',
                    zoomed ? 'max-w-none' : 'max-h-full max-w-full object-contain'
                  )}
                  data-testid="image-lightbox-image"
                  data-zoomed={zoomed ? 'true' : 'false'}
                />
              </button>
            </div>

            {images.length > 1 && (
              <button
                type="button"
                className={cn(overlayButtonClass, 'absolute right-3 z-10')}
                onClick={() => step(1)}
                aria-label="Next image"
                data-testid="image-lightbox-next"
              >
                <ChevronRightIcon size={20} />
              </button>
            )}
          </div>

          <div className="flex items-center justify-center gap-3 p-3 text-sm text-white">
            <span className="max-w-[70vw] truncate" data-testid="image-lightbox-caption">
              {image.name}
            </span>
            {images.length > 1 && (
              <span className="text-white/70" data-testid="image-lightbox-counter">
                {index + 1} / {images.length}
              </span>
            )}
          </div>
        </DialogPrimitiveContent>
      </DialogPortal>
    </Dialog>
  );
};
