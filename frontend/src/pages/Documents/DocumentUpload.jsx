import React, { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import styles from './Documents.module.css';

const DocumentUpload = () => {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);

  const [formData, setFormData] = useState({
    source_type: 'manual_upload',
    source_id: '',
    source_uri: '',
    title: '',
    created_at: '',
    metadata: '',
    content_parts: '',
    url: '',
  });

  const [file, setFile] = useState(null);
  const [uploadType, setUploadType] = useState('file'); // 'file', 'url', or 'content'

  const handleInputChange = (e) => {
    const { name, value } = e.target;
    setFormData((prev) => ({
      ...prev,
      [name]: value,
    }));
  };

  const handleFileChange = (e) => {
    setFile(e.target.files[0]);
  };

  const handleUploadTypeChange = (type) => {
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

  const handleSubmit = async (e) => {
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
        form.append('created_at', formData.created_at);
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
    } catch (err) {
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

      {error && <div className={styles.error}>Error: {error}</div>}
      {success && <div className={styles.success}>{success}</div>}

      <form onSubmit={handleSubmit} className={styles.uploadForm}>
        <div className={styles.formSection}>
          <h2>Upload Type</h2>
          <div className={styles.uploadTypeButtons}>
            <button
              type="button"
              className={`${styles.typeButton} ${uploadType === 'file' ? styles.active : ''}`}
              onClick={() => handleUploadTypeChange('file')}
            >
              Upload File
            </button>
            <button
              type="button"
              className={`${styles.typeButton} ${uploadType === 'url' ? styles.active : ''}`}
              onClick={() => handleUploadTypeChange('url')}
            >
              Scrape URL
            </button>
            <button
              type="button"
              className={`${styles.typeButton} ${uploadType === 'content' ? styles.active : ''}`}
              onClick={() => handleUploadTypeChange('content')}
            >
              Manual Content
            </button>
          </div>
        </div>

        <div className={styles.formSection}>
          <h2>Document Content</h2>

          {uploadType === 'file' && (
            <div className={styles.formGroup}>
              <label htmlFor="file">Select File *</label>
              <input
                type="file"
                id="file"
                onChange={handleFileChange}
                required={uploadType === 'file'}
                accept=".pdf,.txt,.docx,.doc,.html,.md"
              />
              <small>Supported formats: PDF, TXT, DOCX, DOC, HTML, MD</small>
            </div>
          )}

          {uploadType === 'url' && (
            <div className={styles.formGroup}>
              <label htmlFor="url">URL to Scrape *</label>
              <input
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
            <div className={styles.formGroup}>
              <label htmlFor="content_parts">Content Parts (JSON) *</label>
              <textarea
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
              <small>
                Enter content as JSON object with keys like "title", "content", "summary", etc.
              </small>
            </div>
          )}
        </div>

        <div className={styles.formSection}>
          <h2>Document Metadata</h2>

          <div className={styles.formGroup}>
            <label htmlFor="title">Title *</label>
            <input
              type="text"
              id="title"
              name="title"
              value={formData.title}
              onChange={handleInputChange}
              placeholder="Document Title"
              required
            />
          </div>

          <div className={styles.formGroup}>
            <label htmlFor="source_type">Source Type *</label>
            <select
              id="source_type"
              name="source_type"
              value={formData.source_type}
              onChange={handleInputChange}
              required
            >
              <option value="manual_upload">Manual Upload</option>
              <option value="scanned_receipt">Scanned Receipt</option>
              <option value="email_attachment">Email Attachment</option>
              <option value="web_scrape">Web Scrape</option>
              <option value="note">Note</option>
              <option value="other">Other</option>
            </select>
          </div>

          <div className={styles.formGroup}>
            <label htmlFor="source_id">Source ID (optional)</label>
            <input
              type="text"
              id="source_id"
              name="source_id"
              value={formData.source_id}
              onChange={handleInputChange}
              placeholder="Auto-generated if empty"
            />
            <small>Unique identifier within the source type</small>
          </div>

          <div className={styles.formGroup}>
            <label htmlFor="source_uri">Source URI (optional)</label>
            <input
              type="text"
              id="source_uri"
              name="source_uri"
              value={formData.source_uri}
              onChange={handleInputChange}
              placeholder="Auto-generated if empty"
            />
            <small>Canonical URI/URL of the original document</small>
          </div>

          <div className={styles.formGroup}>
            <label htmlFor="created_at">Created At (optional)</label>
            <input
              type="datetime-local"
              id="created_at"
              name="created_at"
              value={formData.created_at}
              onChange={handleInputChange}
            />
            <small>Original creation timestamp</small>
          </div>

          <div className={styles.formGroup}>
            <label htmlFor="metadata">Additional Metadata (JSON, optional)</label>
            <textarea
              id="metadata"
              name="metadata"
              value={formData.metadata}
              onChange={handleInputChange}
              rows={5}
              placeholder={'{\n  "author": "John Doe",\n  "category": "Research"\n}'}
            />
            <small>Additional metadata as JSON object</small>
          </div>
        </div>

        <div className={styles.formActions}>
          <button type="submit" className={styles.submitButton} disabled={loading}>
            {loading ? 'Uploading...' : 'Upload Document'}
          </button>
          <Link to="/documents" className={styles.cancelButton}>
            Cancel
          </Link>
        </div>
      </form>
    </div>
  );
};

export default DocumentUpload;
