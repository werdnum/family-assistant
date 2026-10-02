import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { PageContainer, PageHeader } from '@/components/layout/PageHeader';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import ArtifactConfirmation, {
  artifactStatus,
  loadArtifacts,
} from '../Artifacts/ArtifactConfirmation';

const emptyForm = { name: '', description: '', script_code: '', parameters_schema: '' };

const ScriptsPage = () => {
  const [scripts, setScripts] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [editing, setEditing] = useState(false);
  const [selected, setSelected] = useState(null);
  const [form, setForm] = useState(emptyForm);
  const [saving, setSaving] = useState(false);

  const reload = async () => {
    setLoading(true);
    setError(null);
    try {
      const artifacts = await loadArtifacts();
      setScripts(artifacts.filter((artifact) => artifact.kind === 'script'));
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => {
    reload();
    document.title = 'Scripts - Family Assistant';
  }, []);

  useEffect(() => {
    const root = document.getElementById('app-root');
    if (!loading) {
      root?.setAttribute('data-app-ready', 'true');
    }
    return () => root?.removeAttribute('data-app-ready');
  }, [loading]);

  const edit = (script) => {
    setSelected(script);
    setForm(
      script
        ? {
            name: script.name,
            description: script.content.description,
            script_code: script.content.script_code,
            parameters_schema: script.content.parameters_schema
              ? JSON.stringify(script.content.parameters_schema, null, 2)
              : '',
          }
        : emptyForm
    );
    setError(null);
    setEditing(true);
  };

  const save = async (event) => {
    event.preventDefault();
    setError(null);
    setSaving(true);
    try {
      const schema = form.parameters_schema.trim() ? JSON.parse(form.parameters_schema) : null;
      if (schema !== null && (typeof schema !== 'object' || Array.isArray(schema))) {
        throw new Error('Parameter schema must be a JSON object.');
      }
      const response = await fetch('/api/scripts/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...form,
          parameters_schema: schema,
          expected_content_hash: selected?.content_hash ?? null,
        }),
      });
      if (!response.ok) {
        const data = await response.json();
        throw new Error(
          typeof data.detail === 'string'
            ? data.detail
            : 'Failed to save script. Check the required fields.'
        );
      }
      setEditing(false);
      setSelected(null);
      await reload();
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <PageContainer className="space-y-6 max-w-5xl">
      <PageHeader
        title="Scripts"
        description="Create and edit reusable Python scripts. Saving validates the script and stamps it as your own edit."
      />
      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      {editing ? (
        <Card>
          <CardHeader>
            <CardTitle>{selected ? `Edit ${selected.name}` : 'New script'}</CardTitle>
          </CardHeader>
          <CardContent>
            <form onSubmit={save} className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="script-name">Name</Label>
                <Input
                  id="script-name"
                  required
                  value={form.name}
                  disabled={saving || Boolean(selected)}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="script-description">Description</Label>
                <Textarea
                  id="script-description"
                  value={form.description}
                  disabled={saving}
                  onChange={(e) => setForm({ ...form, description: e.target.value })}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="script-code">Code</Label>
                <Textarea
                  id="script-code"
                  className="min-h-80 font-mono"
                  spellCheck={false}
                  required
                  value={form.script_code}
                  disabled={saving}
                  onChange={(e) => setForm({ ...form, script_code: e.target.value })}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="script-schema">Parameter schema (JSON, optional)</Label>
                <Textarea
                  id="script-schema"
                  className="font-mono"
                  rows={8}
                  spellCheck={false}
                  value={form.parameters_schema}
                  disabled={saving}
                  onChange={(e) => setForm({ ...form, parameters_schema: e.target.value })}
                />
              </div>
              <p className="text-sm text-muted-foreground">
                Changes apply to every automation that references this script.
              </p>
              <div className="flex gap-3">
                <Button type="submit" disabled={saving}>
                  {saving ? 'Saving...' : 'Save script'}
                </Button>
                <Button
                  type="button"
                  variant="secondary"
                  disabled={saving}
                  onClick={() => setEditing(false)}
                >
                  Cancel
                </Button>
              </div>
            </form>
          </CardContent>
        </Card>
      ) : (
        <>
          <div className="flex flex-wrap gap-3">
            <Button onClick={() => edit(null)}>New script</Button>
            <Button variant="outline" onClick={reload} disabled={loading}>
              Reload
            </Button>
            <Button variant="outline" asChild>
              <Link to="/artifacts?kind=script">Artifact review</Link>
            </Button>
          </div>
          {loading ? (
            <p role="status">Loading scripts...</p>
          ) : scripts.length === 0 ? (
            <p>No stored scripts.</p>
          ) : (
            scripts.map((script) => (
              <Card key={script.id}>
                <CardHeader>
                  <CardTitle className="break-words text-lg">{script.name}</CardTitle>
                </CardHeader>
                <CardContent className="space-y-3">
                  <p className="whitespace-pre-wrap text-sm">{script.content.description}</p>
                  <div className="flex flex-wrap items-center gap-3">
                    <Badge variant="secondary">{artifactStatus(script)}</Badge>
                    <Button variant="outline" onClick={() => edit(script)}>
                      Edit script
                    </Button>
                    <ArtifactConfirmation
                      artifact={script}
                      onConfirmed={(updated) =>
                        setScripts((current) =>
                          current.map((item) => (item.id === updated.id ? updated : item))
                        )
                      }
                    />
                  </div>
                </CardContent>
              </Card>
            ))
          )}
        </>
      )}
    </PageContainer>
  );
};

export default ScriptsPage;
