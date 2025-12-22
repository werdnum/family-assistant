import React, { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { AttachmentPreview } from './AttachmentPreview';
import { AttachmentUpload } from './AttachmentUpload';

const NotesForm = ({ isEdit, onSuccess, onCancel }) => {
  const { title: urlTitle } = useParams();
  const [formData, setFormData] = useState({
    title: '',
    content: '',
    include_in_prompt: true,
    attachment_ids: [],
  });
  const [originalTitle, setOriginalTitle] = useState(null); // Track original title for edits
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [initialLoading, setInitialLoading] = useState(isEdit);

  useEffect(() => {
    if (isEdit && urlTitle) {
      fetchNote(urlTitle);
    }
  }, [isEdit, urlTitle]);

  const fetchNote = async (title) => {
    try {
      setInitialLoading(true);
      const response = await fetch(`/api/notes/${encodeURIComponent(title)}`);
      if (!response.ok) {
        throw new Error(`Failed to fetch note: ${response.status}`);
      }
      const note = await response.json();
      setFormData({
        title: note.title,
        content: note.content,
        include_in_prompt: note.include_in_prompt,
        attachment_ids: note.attachment_ids || [],
      });
      setOriginalTitle(note.title); // Remember the original title
    } catch (err) {
      setError(err.message);
    } finally {
      setInitialLoading(false);
    }
  };

  const handleChange = (e) => {
    const { name, value, type, checked } = e.target;
    setFormData((prev) => ({
      ...prev,
      [name]: type === 'checkbox' ? checked : value,
    }));
  };

  const handleCheckboxChange = (checked) => {
    setFormData((prev) => ({
      ...prev,
      include_in_prompt: checked,
    }));
  };

  const handleAttachmentUpload = (attachmentId) => {
    setFormData((prev) => ({
      ...prev,
      attachment_ids: [...prev.attachment_ids, attachmentId],
    }));
  };

  const handleAttachmentRemove = (attachmentId) => {
    setFormData((prev) => ({
      ...prev,
      attachment_ids: prev.attachment_ids.filter((id) => id !== attachmentId),
    }));
  };

  const handleSubmit = async (e) => {
    e.preventDefault();

    if (!formData.title.trim() || !formData.content.trim()) {
      setError('Title and content are required');
      return;
    }

    try {
      setLoading(true);
      setError(null);

      // Prepare the request body, including original_title for edits
      const requestBody = {
        ...formData,
        ...(isEdit && originalTitle ? { original_title: originalTitle } : {}),
      };

      const response = await fetch('/api/notes/', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(requestBody),
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || `HTTP error! status: ${response.status}`);
      }

      onSuccess();
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  if (initialLoading) {
    return (
      <div className="flex items-center justify-center p-8">
        <div>Loading note...</div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">{isEdit ? 'Edit Note' : 'Add New Note'}</h1>

      {error && (
        <Alert variant="destructive">
          <AlertDescription>Error: {error}</AlertDescription>
        </Alert>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Note Details</CardTitle>
        </CardHeader>
        <CardContent className="space-y-6">
          <form onSubmit={handleSubmit} className="space-y-6">
            <div className="space-y-2">
              <Label htmlFor="title">Title *</Label>
              <Input
                type="text"
                id="title"
                name="title"
                value={formData.title}
                onChange={handleChange}
                required
                disabled={loading}
                placeholder="Enter note title"
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="content">Content *</Label>
              <Textarea
                id="content"
                name="content"
                value={formData.content}
                onChange={handleChange}
                required
                disabled={loading}
                rows={20}
                className="font-mono"
                placeholder="Enter note content..."
              />
            </div>

            <div className="space-y-3">
              <div className="flex items-center space-x-2">
                <Checkbox
                  id="include_in_prompt"
                  checked={formData.include_in_prompt}
                  onCheckedChange={handleCheckboxChange}
                  disabled={loading}
                />
                <Label htmlFor="include_in_prompt" className="font-normal">
                  Include in system prompt
                </Label>
              </div>
              <p className="text-sm text-muted-foreground pl-6">
                When enabled, this note will be included in the system prompt for LLM conversations.
              </p>
            </div>

            <div className="space-y-3">
              <Label>Attachments</Label>
              <AttachmentUpload onUploadComplete={handleAttachmentUpload} disabled={loading} />
              {formData.attachment_ids.length > 0 && (
                <div className="flex flex-wrap gap-3 mt-3">
                  {formData.attachment_ids.map((attachmentId) => (
                    <AttachmentPreview
                      key={attachmentId}
                      attachmentId={attachmentId}
                      onRemove={handleAttachmentRemove}
                      canRemove={!loading}
                    />
                  ))}
                </div>
              )}
              <p className="text-sm text-muted-foreground">
                Attach images, documents, or other files to this note. Supported formats: images,
                text, markdown, PDF.
              </p>
            </div>

            <div className="flex gap-4 pt-6">
              <Button type="submit" disabled={loading}>
                {loading ? 'Saving...' : 'Save'}
              </Button>
              <Button type="button" variant="secondary" onClick={onCancel} disabled={loading}>
                Cancel
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  );
};

export default NotesForm;
