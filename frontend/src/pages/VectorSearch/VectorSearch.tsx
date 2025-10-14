import React, { useState, useEffect, ChangeEvent, FormEvent } from 'react';
import { Link } from 'react-router-dom';
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
import { Checkbox } from '@/components/ui/checkbox';
import { Card, CardContent } from '@/components/ui/card';
import { Alert, AlertDescription } from '@/components/ui/alert';
import styles from './VectorSearch.module.css';

interface SearchFilters {
  source_types: string[];
  embedding_types: string[];
  created_after: string;
  created_before: string;
  title_like: string;
  metadata_filters: Record<string, string>;
}

interface SearchParams {
  query_text: string;
  limit: number;
  filters: SearchFilters;
}

interface MetadataFilterRow {
  key: string;
  value: string;
  id: number;
}

interface SearchResultDocument {
  id: string;
  source_uri?: string;
  title?: string;
  source_type: string;
  created_at: string;
  metadata?: Record<string, any>;
}

interface SearchResult {
  document: SearchResultDocument;
  score: number;
}

interface AvailableOptions {
  models: string[];
  types: string[];
  source_types: string[];
  metadata_keys: string[];
}

const VectorSearch: React.FC = () => {
  const [searchParams, setSearchParams] = useState<SearchParams>({
    query_text: '',
    limit: 10,
    filters: {
      source_types: [],
      embedding_types: [],
      created_after: '',
      created_before: '',
      title_like: '',
      metadata_filters: {},
    },
  });

  const [advancedMode, setAdvancedMode] = useState(false);
  const [metadataFilterRows, setMetadataFilterRows] = useState<MetadataFilterRow[]>([]);
  const [results, setResults] = useState<SearchResult[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Available options fetched from the backend
  const [availableOptions, setAvailableOptions] = useState<AvailableOptions>({
    models: [],
    types: [],
    source_types: [],
    metadata_keys: [],
  });

  // Fetch available filter options on mount
  useEffect(() => {
    const fetchOptions = async () => {
      try {
        // We need to get these from the documents API
        const docsResponse = await fetch('/api/documents/?limit=0');
        if (docsResponse.ok) {
          // This gives us a basic idea, but we might need a dedicated endpoint
          // For now, we'll use hardcoded common values
          setAvailableOptions({
            models: [], // Will be populated when we have a proper endpoint
            types: ['content_chunk', 'raw_note_text', 'raw_file_text'],
            source_types: ['manual_upload', 'manual_test_upload', 'api_generated'],
            metadata_keys: ['author', 'category', 'priority'],
          });
        }
      } catch (err) {
        console.error('Error fetching filter options:', err);
      }
    };

    fetchOptions();
  }, []);

  const handleInputChange = (e: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => {
    const { name, value } = e.target;
    if (name === 'query_text' || name === 'limit') {
      setSearchParams((prev) => ({
        ...prev,
        [name]: name === 'limit' ? parseInt(value, 10) : value,
      }));
    } else if (name.startsWith('filters.')) {
      const filterName = name.substring(8);
      setSearchParams((prev) => ({
        ...prev,
        filters: { ...prev.filters, [filterName]: value },
      }));
    }
  };

  const handleCheckboxChange = (
    category: keyof Pick<SearchFilters, 'source_types' | 'embedding_types'>,
    value: string,
    checked: boolean | 'indeterminate'
  ) => {
    setSearchParams((prev) => {
      const currentValues = prev.filters[category] || [];
      const newValues = checked
        ? [...currentValues, value]
        : currentValues.filter((v) => v !== value);
      return {
        ...prev,
        filters: { ...prev.filters, [category]: newValues },
      };
    });
  };

  const addMetadataFilter = () => {
    setMetadataFilterRows((prev) => [...prev, { key: '', value: '', id: Date.now() }]);
  };

  const updateMetadataFilter = (id: number, field: 'key' | 'value', value: string) => {
    setMetadataFilterRows((prev) =>
      prev.map((row) => (row.id === id ? { ...row, [field]: value } : row))
    );
  };

  const removeMetadataFilter = (id: number) => {
    setMetadataFilterRows((prev) => prev.filter((row) => row.id !== id));
  };

  const handleSearch = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!searchParams.query_text.trim()) {
      setError('Please enter a search query');
      return;
    }

    setLoading(true);
    setError(null);
    setResults(null);

    try {
      // Build metadata filters object from rows
      const metadataFilters: Record<string, string> = {};
      metadataFilterRows.forEach((row) => {
        if (row.key && row.value) {
          metadataFilters[row.key] = row.value;
        }
      });

      // Build request payload
      const payload: any = {
        query_text: searchParams.query_text,
        limit: searchParams.limit || 10,
        filters: {
          source_types: searchParams.filters.source_types,
          embedding_types: searchParams.filters.embedding_types,
          created_after: searchParams.filters.created_after
            ? new Date(searchParams.filters.created_after).toISOString()
            : null,
          created_before: searchParams.filters.created_before
            ? new Date(searchParams.filters.created_before).toISOString()
            : null,
          title_like: searchParams.filters.title_like || null,
          metadata_filters: metadataFilters,
        },
      };

      // Remove empty arrays and null values from filters
      Object.keys(payload.filters).forEach((key) => {
        const filterKey = key as keyof SearchFilters;
        if (Array.isArray(payload.filters[filterKey]) && payload.filters[filterKey].length === 0) {
          delete payload.filters[filterKey];
        } else if (payload.filters[filterKey] === null || payload.filters[filterKey] === '') {
          delete payload.filters[filterKey];
        }
      });

      const response = await fetch('/api/vector-search/', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(payload),
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || 'Search failed');
      }

      const data = await response.json();
      setResults(data);
    } catch (err: any) {
      console.error('Search error:', err);
      setError(err.message || 'An error occurred during search');
    } finally {
      setLoading(false);
    }
  };

  const formatMetadata = (metadata: Record<string, any> | undefined) => {
    if (!metadata || Object.keys(metadata).length === 0) {
      return 'None';
    }
    return JSON.stringify(metadata, null, 2);
  };

  const formatDate = (dateString: string | undefined) => {
    if (!dateString) {
      return 'N/A';
    }
    return new Date(dateString).toLocaleString();
  };

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <h1>Vector Search</h1>
      </div>

      <Card className="mb-6">
        <CardContent className="pt-6">
          <form className={styles.searchForm} onSubmit={handleSearch}>
            <div className="grid gap-4">
              <div className="space-y-2">
                <Label htmlFor="query_text">Search Query</Label>
                <Textarea
                  id="query_text"
                  name="query_text"
                  placeholder="Describe what you're looking for..."
                  value={searchParams.query_text}
                  onChange={handleInputChange}
                  rows={3}
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="limit">Result Limit</Label>
                <Input
                  type="number"
                  id="limit"
                  name="limit"
                  value={searchParams.limit}
                  onChange={handleInputChange}
                  min="1"
                  max="100"
                />
              </div>

              <div className="space-y-2">
                <Label>Filter by Source Type</Label>
                <div className="flex flex-wrap gap-4">
                  {availableOptions.source_types.map((type) => (
                    <div key={type} className="flex items-center space-x-2">
                      <Checkbox
                        id={`source_type_${type}`}
                        checked={searchParams.filters.source_types.includes(type)}
                        onCheckedChange={(checked) =>
                          handleCheckboxChange('source_types', type, checked)
                        }
                      />
                      <Label
                        htmlFor={`source_type_${type}`}
                        className="text-sm font-normal cursor-pointer"
                      >
                        {type}
                      </Label>
                    </div>
                  ))}
                </div>
              </div>

              <div className="space-y-2">
                <Label htmlFor="title_like">Title Contains</Label>
                <Input
                  type="text"
                  id="title_like"
                  name="filters.title_like"
                  placeholder="Filter by title..."
                  value={searchParams.filters.title_like}
                  onChange={handleInputChange}
                />
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div className="space-y-2">
                  <Label htmlFor="created_after">Created After</Label>
                  <Input
                    type="date"
                    id="created_after"
                    name="filters.created_after"
                    value={searchParams.filters.created_after}
                    onChange={handleInputChange}
                  />
                </div>

                <div className="space-y-2">
                  <Label htmlFor="created_before">Created Before</Label>
                  <Input
                    type="date"
                    id="created_before"
                    name="filters.created_before"
                    value={searchParams.filters.created_before}
                    onChange={handleInputChange}
                  />
                </div>
              </div>

              <div>
                <details className={styles.advancedOptions} open={advancedMode}>
                  <summary onClick={() => setAdvancedMode(!advancedMode)}>Advanced Options</summary>
                  <div className="space-y-4 mt-4">
                    <div className="space-y-2">
                      <Label>Filter by Embedding Type</Label>
                      <div className="flex flex-wrap gap-4">
                        {availableOptions.types.map((type) => (
                          <div key={type} className="flex items-center space-x-2">
                            <Checkbox
                              id={`embedding_type_${type}`}
                              checked={searchParams.filters.embedding_types.includes(type)}
                              onCheckedChange={(checked) =>
                                handleCheckboxChange('embedding_types', type, checked)
                              }
                            />
                            <Label
                              htmlFor={`embedding_type_${type}`}
                              className="text-sm font-normal cursor-pointer"
                            >
                              {type}
                            </Label>
                          </div>
                        ))}
                      </div>
                    </div>

                    <div className="space-y-2">
                      <Label>Metadata Filters</Label>
                      <div className="space-y-2">
                        {metadataFilterRows.map((row) => (
                          <div key={row.id} className="flex gap-2 items-end">
                            <div className="flex-1">
                              <Select
                                value={row.key}
                                onValueChange={(value) =>
                                  updateMetadataFilter(row.id, 'key', value)
                                }
                              >
                                <SelectTrigger>
                                  <SelectValue placeholder="Select Key" />
                                </SelectTrigger>
                                <SelectContent>
                                  {availableOptions.metadata_keys.map((key) => (
                                    <SelectItem key={key} value={key}>
                                      {key}
                                    </SelectItem>
                                  ))}
                                </SelectContent>
                              </Select>
                            </div>
                            <div className="flex-1">
                              <Input
                                type="text"
                                placeholder="Value"
                                value={row.value}
                                onChange={(e) =>
                                  updateMetadataFilter(row.id, 'value', e.target.value)
                                }
                              />
                            </div>
                            <Button
                              type="button"
                              variant="destructive"
                              size="sm"
                              onClick={() => removeMetadataFilter(row.id)}
                            >
                              Remove
                            </Button>
                          </div>
                        ))}
                        <Button type="button" variant="secondary" onClick={addMetadataFilter}>
                          Add Metadata Filter
                        </Button>
                      </div>
                    </div>
                  </div>
                </details>
              </div>

              <Button type="submit" className="w-full" disabled={loading}>
                {loading ? 'Searching...' : 'Search'}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>

      {error && (
        <Alert variant="destructive" className="mb-4">
          <AlertDescription>Error: {error}</AlertDescription>
        </Alert>
      )}

      {loading && (
        <Alert className="mb-4">
          <AlertDescription>Searching...</AlertDescription>
        </Alert>
      )}

      {results !== null && !loading && (
        <div className={styles.results}>
          <h2 className={styles.resultsHeader}>
            Results {results.length > 0 && `(${results.length})`}
          </h2>
          {results.length === 0 ? (
            <div className={styles.noResults}>No results found for your search.</div>
          ) : (
            <div>
              {results.map((result) => (
                <article key={result.document.id} className={styles.resultCard}>
                  <h3 className={styles.resultTitle}>
                    {result.document.source_uri ? (
                      <a
                        href={result.document.source_uri}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        {result.document.title || 'Untitled Document'}
                      </a>
                    ) : (
                      result.document.title || 'Untitled Document'
                    )}
                    <span className={styles.score}>Score: {result.score.toFixed(4)}</span>
                  </h3>

                  <div className={styles.sourceInfo}>
                    Source: {result.document.source_type} | Created:{' '}
                    {formatDate(result.document.created_at)}
                    <br />
                    <Link to={`/documents/${result.document.id}`} className={styles.documentLink}>
                      View Full Document Details (ID: {result.document.id})
                    </Link>
                  </div>

                  {result.document.metadata && Object.keys(result.document.metadata).length > 0 && (
                    <details className={styles.metadataSection}>
                      <summary>Document Metadata</summary>
                      <div className={styles.metadataContent}>
                        {formatMetadata(result.document.metadata)}
                      </div>
                    </details>
                  )}
                </article>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export default VectorSearch;
