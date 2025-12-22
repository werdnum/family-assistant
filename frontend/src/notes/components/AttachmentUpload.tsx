import { PaperclipIcon, Loader2Icon } from 'lucide-react';
import React, { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Alert, AlertDescription } from '@/components/ui/alert';

interface AttachmentUploadProps {
  onUploadComplete: (attachmentId: string) => void;
  disabled?: boolean;
}

// Supported file types matching backend
const SUPPORTED_FILE_TYPES = [
  'image/jpeg',
  'image/png',
  'image/gif',
  'image/webp',
  'text/plain',
  'text/markdown',
  'application/pdf',
];

const MAX_FILE_SIZE = 100 * 1024 * 1024; // 100MB

/**
 * Component for uploading attachments to notes.
 * Handles file validation and upload to the attachment service.
 */
export const AttachmentUpload: React.FC<AttachmentUploadProps> = ({
  onUploadComplete,
  disabled = false,
}) => {
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileInputRef = React.useRef<HTMLInputElement>(null);

  const validateFile = (file: File): string | null => {
    if (file.size > MAX_FILE_SIZE) {
      return `File size exceeds ${MAX_FILE_SIZE / (1024 * 1024)}MB limit`;
    }

    if (!SUPPORTED_FILE_TYPES.includes(file.type)) {
      return `Unsupported file type. Supported: images, text, markdown, PDF`;
    }

    if (!file.name || file.name.trim() === '') {
      return 'File must have a valid name';
    }

    return null;
  };

  const uploadFile = async (file: File): Promise<string> => {
    const formData = new FormData();
    formData.append('file', file);

    const response = await fetch('/api/attachments/upload', {
      method: 'POST',
      body: formData,
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({ detail: 'Upload failed' }));
      throw new Error(errorData.detail || `Upload failed with status ${response.status}`);
    }

    const result = await response.json();
    return result.attachment_id;
  };

  const handleFileSelect = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = event.target.files;
    if (!files || files.length === 0) {
      return;
    }

    setError(null);

    // Process files sequentially
    for (const file of Array.from(files)) {
      const validationError = validateFile(file);
      if (validationError) {
        setError(validationError);
        continue;
      }

      try {
        setUploading(true);
        const attachmentId = await uploadFile(file);
        onUploadComplete(attachmentId);
      } catch (err) {
        const message = err instanceof Error ? err.message : 'Upload failed';
        setError(message);
      } finally {
        setUploading(false);
      }
    }

    // Clear the input so the same file can be selected again
    if (fileInputRef.current) {
      fileInputRef.current.value = '';
    }
  };

  return (
    <div className="space-y-2">
      <input
        ref={fileInputRef}
        type="file"
        accept={SUPPORTED_FILE_TYPES.join(',')}
        multiple
        className="hidden"
        onChange={handleFileSelect}
        disabled={disabled || uploading}
      />
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() => fileInputRef.current?.click()}
        disabled={disabled || uploading}
      >
        {uploading ? (
          <>
            <Loader2Icon className="mr-2 size-4 animate-spin" />
            Uploading...
          </>
        ) : (
          <>
            <PaperclipIcon className="mr-2 size-4" />
            Add Attachments
          </>
        )}
      </Button>
      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
    </div>
  );
};
