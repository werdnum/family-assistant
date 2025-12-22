import { FileIcon, ImageIcon, XIcon } from 'lucide-react';
import React, { useState } from 'react';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogOverlay,
  DialogPortal,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';

interface AttachmentPreviewProps {
  attachmentId: string;
  onRemove?: (attachmentId: string) => void;
  canRemove?: boolean;
}

/**
 * Component for displaying a single attachment with preview thumbnail for images
 * and file icon for other types.
 */
export const AttachmentPreview: React.FC<AttachmentPreviewProps> = ({
  attachmentId,
  onRemove,
  canRemove = true,
}) => {
  const [metadata, setMetadata] = useState<{
    mime_type: string;
    filename: string;
    size: number;
  } | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [imageLoaded, setImageLoaded] = useState(false);

  // Fetch attachment metadata on mount
  React.useEffect(() => {
    const fetchMetadata = async () => {
      try {
        const response = await fetch(`/api/attachments/${attachmentId}`, {
          headers: {
            Accept: 'application/json',
          },
        });
        if (response.ok) {
          const data = await response.json();
          setMetadata({
            mime_type: data.mime_type,
            filename: data.filename || 'attachment',
            size: data.size || 0,
          });
        }
      } catch (error) {
        console.error('Error fetching attachment metadata:', error);
        setLoadError(true);
      }
    };

    fetchMetadata();
  }, [attachmentId]);

  const isImage = metadata?.mime_type?.startsWith('image/');
  const attachmentUrl = `/api/attachments/${attachmentId}`;

  const formatFileSize = (bytes: number): string => {
    if (bytes < 1024) {
      return `${bytes} B`;
    }
    if (bytes < 1024 * 1024) {
      return `${(bytes / 1024).toFixed(1)} KB`;
    }
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  const handleRemove = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (onRemove) {
      onRemove(attachmentId);
    }
  };

  if (loadError) {
    return (
      <div className="relative flex h-20 w-32 items-center justify-center gap-2 rounded-lg border border-red-300 bg-red-50 p-2">
        <FileIcon className="size-6 text-red-500" />
        <span className="text-xs text-red-600">Error loading</span>
      </div>
    );
  }

  if (!metadata) {
    return (
      <div className="relative flex h-20 w-32 items-center justify-center gap-2 rounded-lg border border-gray-200 bg-gray-50 p-2 animate-pulse">
        <FileIcon className="size-6 text-gray-400" />
        <span className="text-xs text-gray-500">Loading...</span>
      </div>
    );
  }

  const PreviewContent = () => (
    <div className="relative flex h-20 w-32 flex-col rounded-lg border border-gray-300 overflow-hidden hover:border-gray-400 transition-colors">
      {isImage ? (
        <Dialog>
          <DialogTrigger asChild>
            <div className="relative flex h-full cursor-pointer items-center justify-center bg-gray-50">
              {!imageLoaded && (
                <div className="absolute inset-0 flex items-center justify-center">
                  <ImageIcon className="size-6 text-gray-400" />
                </div>
              )}
              <img
                src={attachmentUrl}
                alt={metadata.filename}
                className="h-full w-full object-cover"
                onLoad={() => setImageLoaded(true)}
                onError={() => setLoadError(true)}
                style={{ display: imageLoaded ? 'block' : 'none' }}
              />
            </div>
          </DialogTrigger>
          <DialogPortal>
            <DialogOverlay />
            <DialogContent className="max-w-[90vw] max-h-[90vh]">
              <DialogTitle>{metadata.filename}</DialogTitle>
              <img
                src={attachmentUrl}
                alt={metadata.filename}
                className="max-w-full max-h-[80vh] object-contain"
              />
            </DialogContent>
          </DialogPortal>
        </Dialog>
      ) : (
        <div className="flex h-full items-center justify-center gap-2 bg-gray-50 p-2">
          <FileIcon className="size-6 text-gray-600" />
          <div className="flex flex-col text-xs overflow-hidden">
            <span className="font-medium truncate" title={metadata.filename}>
              {metadata.filename}
            </span>
            <span className="text-gray-500">{formatFileSize(metadata.size)}</span>
          </div>
        </div>
      )}
      {canRemove && onRemove && (
        <Button
          size="sm"
          variant="destructive"
          className="absolute -right-2 -top-2 size-6 rounded-full p-0"
          onClick={handleRemove}
          title="Remove attachment"
        >
          <XIcon className="size-4" />
        </Button>
      )}
    </div>
  );

  return <PreviewContent />;
};
