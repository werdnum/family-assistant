import React, { useState, ChangeEvent, FormEvent } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Alert, AlertDescription } from '@/components/ui/alert';
import styles from './Documents.module.css';

type UploadType = 'file' | 'url' | 'content';

interface FormData {
  source_type: string;
  source_id: string;
  source_uri: string;
  title: string;
  created_at: string;
  metadata: string;
  content_parts: string;
  url: string;
}

const DocumentUpload: React.FC = () => {
  const navigate = useNavigate();
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const [formData, setFormData] = useState<FormData>({
    source_type: 'manual_upload',
    source_id: '',
    source_uri: '',
    title: '',
    created_at: '',
    metadata: '',
    content_parts: '',
    url: '',
  });

  const [file, setFile] = useState<File | null>(null);
  const [uploadType, setUploadType] = useState<UploadType>('file');

  const handleInputChange = (e: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => {
    const { name, value } = e.target;
    setFormData((prev) => ({
      ...prev,
      [name]: value,
    }));
  };

  const handleSelectChange = (name: keyof FormData, value: string) => {
    setFormData((prev) => ({
      ...prev,
      [name]: value,
    }));
  };

  const handleFileChange = (e: ChangeEvent<HTMLInputElement>) => {
    if (e.target.files) {
      setFile(e.target.files[0]);
    }
  };

  const handleUploadTypeChange = (type: UploadType) => {
    setUploadType(type);
    // Clear the unused fields when switching types
    if (type === 'file') {
      setFormData((prev) => ({ ...prev, url: '', content_parts: '' }));
    } else if (type === 'url') {
      setFile(null);
      setFormData((prev) => ({ ...prev, content_parts: '' }));
    } else if (type === 'content') {
      setFile(null);
      setFormData((prev) => ({ ...prev, url: '' }));
    }
  };

  const handleSubmit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    setSuccess(null);

    try {
      // Generate source_id if not provided
      let sourceId = formData.source_id;
      if (!sourceId) {
        if (uploadType === 'file' && file) {
          sourceId = `file_${file.name}_${Date.now()}`;
        } else if (uploadType === 'url' && formData.url) {
          try {
            const urlObj = new window.URL(formData.url);
            sourceId = `url_${urlObj.hostname}_${Date.now()}`;
          } catch (_urlError) {
            setError('The provided URL is not valid. Please check and try again.');
            setLoading(false);
            return;
          }
        } else {
          sourceId = `manual_${Date.now()}`;
        }
      }

      // Generate source_uri if not provided
      let sourceUri = formData.source_uri;
      if (!sourceUri) {
        if (uploadType === 'file' && file) {
          sourceUri = `file://${file.name}`;
        } else if (uploadType === 'url' && formData.url) {
          sourceUri = formData.url;
        } else {
          sourceUri = `manual://${sourceId}`;
        }
      }

      const form = new window.FormData();
      form.append('source_type', formData.source_type);
      form.append('source_id', sourceId);
      form.append('source_uri', sourceUri);
      form.append('title', formData.title || 'Untitled Document');

      if (formData.created_at) {
        form.append('created_at', new Date(formData.created_at).toISOString());
      }

      if (formData.metadata) {
        form.append('metadata', formData.metadata);
      }

      // Add content based on upload type
      if (uploadType === 'file' && file) {
        form.append('uploaded_file', file);
      } else if (uploadType === 'url' && formData.url) {
        form.append('url', formData.url);
      } else if (uploadType === 'content' && formData.content_parts) {
        form.append('content_parts', formData.content_parts);
      }

      const response = await fetch('/api/documents/upload', {
        method: 'POST',
        body: form,
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.detail || `Upload failed: ${response.statusText}`);
      }

      setSuccess(`Document uploaded successfully! Document ID: ${data.document_id}`);

      // Navigate back to documents list after a short delay
      window.setTimeout(() => {
        navigate('/documents');
      }, 2000);
    } catch (err: any) {
      console.error('Error uploading document:', err);
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <h1>Upload Document</h1>
        <Link to="/documents" className={styles.backButton}>
          Back to Documents
        </Link>
      </div>

      {error && (
        <Alert variant="destructive" className="mb-4">
          <AlertDescription>Error: {error}</AlertDescription>
        </Alert>
      )}
      {success && (
        <Alert className="mb-4">
          <AlertDescription>{success}</AlertDescription>
        </Alert>
      )}

      <form onSubmit={handleSubmit} className={styles.uploadForm}>
        <Card className="mb-6">
          <CardHeader>
            <CardTitle>Upload Type</CardTitle>
          </CardHeader>
          <CardContent>
            <div className={styles.uploadTypeButtons}>
              <Button
                type="button"
                variant={uploadType === 'file' ? 'default' : 'outline'}
                onClick={() => handleUploadTypeChange('file')}
              >
                Upload File
              </Button>
              <Button
                type="button"
                variant={uploadType === 'url' ? 'default' : 'outline'}
                onClick={() => handleUploadTypeChange('url')}
              >
                Scrape URL
              </Button>
              <Button
                type="button"
                variant={uploadType === 'content' ? 'default' : 'outline'}
                onClick={() => handleUploadTypeChange('content')}
              >
                Manual Content
              </Button>
            </div>
          </CardContent>
        </Card>

        <Card className="mb-6">
          <CardHeader>
            <CardTitle>Document Content</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {uploadType === 'file' && (
              <div className="space-y-2">
                <Label htmlFor="file">Select File *</Label>
                <Input
                  type="file"
                  id="file"
                  onChange={handleFileChange}
                  required={uploadType === 'file'}
                  accept=".pdf,.txt,.docx,.doc,.html,.md"
                />
                <p className="text-sm text-muted-foreground">
                  Supported formats: PDF, TXT, DOCX, DOC, HTML, MD
                </p>
              </div>
            )}

            {uploadType === 'url' && (
              <div className="space-y-2">
                <Label htmlFor="url">URL to Scrape *</Label>
                <Input
                  type="url"
                  id="url"
                  name="url"
                  value={formData.url}
                  onChange={handleInputChange}
                  placeholder="https://example.com/document"
                  required={uploadType === 'url'}
                />
              </div>
            )}

            {uploadType === 'content' && (
              <div className="space-y-2">
                <Label htmlFor="content_parts">Content Parts (JSON) *</Label>
                <Textarea
                  id="content_parts"
                  name="content_parts"
                  value={formData.content_parts}
                  onChange={handleInputChange}
                  rows={10}
                  placeholder={
                    '{\n  "title": "Document Title",\n  "content": "Main document content...",\n  "summary": "Brief summary..."\n}'
                  }
                  required={uploadType === 'content'}
                />
                <p className="text-sm text-muted-foreground">
                  Enter content as JSON object with keys like "title", "content", "summary", etc.
                </p>
              </div>
            )}
          </CardContent>
        </Card>

        <Card className="mb-6">
          <CardHeader>
            <CardTitle>Document Metadata</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="title">Title *</Label>
              <Input
                type="text"
                id="title"
                name="title"
                value={formData.title}
                onChange={handleInputChange}
                placeholder="Document Title"
                required
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="source_type">Source Type *</Label>
              <Select
                value={formData.source_type}
                onValueChange={(value) => handleSelectChange('source_type', value)}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="manual_upload">Manual Upload</SelectItem>
                  <SelectItem value="scanned_receipt">Scanned Receipt</SelectItem>
                  <SelectItem value="email_attachment">Email Attachment</SelectItem>
                  <SelectItem value="web_scrape">Web Scrape</SelectItem>
                  <SelectItem value="note">Note</SelectItem>
                  <SelectItem value="other">Other</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-2">
              <Label htmlFor="source_id">Source ID (optional)</Label>
              <Input
                type="text"
                id="source_id"
                name="source_id"
                value={formData.source_id}
                onChange={handleInputChange}
                placeholder="Auto-generated if empty"
              />
              <p className="text-sm text-muted-foreground">
                Unique identifier within the source type
              </p>
            </div>

            <div className="space-y-2">
              <Label htmlFor="source_uri">Source URI (optional)</Label>
              <Input
                type="text"
                id="source_uri"
                name="source_uri"
                value={formData.source_uri}
                onChange={handleInputChange}
                placeholder="Auto-generated if empty"
              />
              <p className="text-sm text-muted-foreground">
                Canonical URI/URL of the original document
              </p>
            </div>

            <div className="space-y-2">
              <Label htmlFor="created_at">Created At (optional)</Label>
              <Input
                type="datetime-local"
                id="created_at"
                name="created_at"
                value={formData.created_at}
                onChange={handleInputChange}
              />
              <p className="text-sm text-muted-foreground">Original creation timestamp</p>
            </div>

            <div className="space-y-2">
              <Label htmlFor="metadata">Additional Metadata (JSON, optional)</Label>
              <Textarea
                id="metadata"
                name="metadata"
                value={formData.metadata}
                onChange={handleInputChange}
                rows={5}
                placeholder={'{\n  "author": "John Doe",\n  "category": "Research"\n}'}
              />
              <p className="text-sm text-muted-foreground">Additional metadata as JSON object</p>
            </div>
          </CardContent>
        </Card>

        <div className={styles.formActions}>
          <Button type="submit" disabled={loading}>
            {loading ? 'Uploading...' : 'Upload Document'}
          </Button>
          <Link to="/documents" className={styles.cancelButton}>
            Cancel
          </Link>
        </div>
      </form>
    </div>
  );
};

export default DocumentUpload;
