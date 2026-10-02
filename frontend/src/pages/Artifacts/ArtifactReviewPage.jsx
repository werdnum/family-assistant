import React, { useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { PageContainer, PageHeader } from '@/components/layout/PageHeader';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import { NativeSelect } from '@/components/ui/native-select';
import ArtifactConfirmation, {
  artifactKindLabels,
  artifactStatus,
  loadArtifacts,
} from './ArtifactConfirmation';

const ArtifactReviewPage = () => {
  const [artifacts, setArtifacts] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [params, setParams] = useSearchParams();
  const kind = params.get('kind') || 'all';
  const [needsReviewOnly, setNeedsReviewOnly] = useState(false);

  const reload = async () => {
    setLoading(true);
    setError(null);
    try {
      setArtifacts(await loadArtifacts());
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => {
    reload();
    document.title = 'Artifact review - Family Assistant';
  }, []);

  const visible = artifacts.filter(
    (artifact) =>
      (kind === 'all' || kind === artifact.kind) &&
      (!needsReviewOnly || artifactStatus(artifact) === 'Needs review')
  );

  return (
    <PageContainer className="space-y-6 max-w-5xl">
      <PageHeader
        title="Artifact review"
        description="Review notes, stored scripts, and automations, then confirm the content you trust."
      />
      <div className="flex flex-wrap items-center gap-4">
        <Label htmlFor="artifact-kind">Type</Label>
        <NativeSelect
          id="artifact-kind"
          value={kind}
          onChange={(e) => setParams({ kind: e.target.value })}
        >
          <option value="all">All artifacts</option>
          {Object.entries(artifactKindLabels).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </NativeSelect>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={needsReviewOnly}
            onChange={(e) => setNeedsReviewOnly(e.target.checked)}
          />
          Needs review only
        </label>
        <Button variant="outline" onClick={reload} disabled={loading}>
          Reload
        </Button>
        <Button variant="outline" asChild>
          <Link to="/scripts">Script editor</Link>
        </Button>
      </div>
      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      {loading ? (
        <p role="status">Loading artifacts...</p>
      ) : visible.length === 0 ? (
        <p>No artifacts match these filters.</p>
      ) : (
        <div className="space-y-3">
          {visible.map((artifact) => (
            <Card key={`${artifact.kind}:${artifact.id}`}>
              <CardHeader className="pb-3">
                <CardTitle className="text-lg break-words">{artifact.name}</CardTitle>
              </CardHeader>
              <CardContent className="flex flex-wrap items-center gap-3">
                <Badge variant="outline">{artifactKindLabels[artifact.kind]}</Badge>
                <Badge
                  variant={
                    artifactStatus(artifact) === 'Needs review' ? 'destructive' : 'secondary'
                  }
                >
                  {artifactStatus(artifact)}
                </Badge>
                <span className="text-xs text-muted-foreground">
                  {artifact.trust_tier.replaceAll('_', ' ')}
                </span>
                <ArtifactConfirmation
                  artifact={artifact}
                  onConfirmed={(updated) =>
                    setArtifacts((current) =>
                      current.map((item) =>
                        item.kind === updated.kind && item.id === updated.id ? updated : item
                      )
                    )
                  }
                />
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </PageContainer>
  );
};

export default ArtifactReviewPage;
