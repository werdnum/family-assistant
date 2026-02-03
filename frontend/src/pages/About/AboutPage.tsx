import React, { useEffect, useState } from 'react';
import { Info, GitBranch, Calendar, Package } from 'lucide-react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';

interface VersionInfo {
  version: string;
  git_commit: string;
  build_date: string;
}

const AboutPage: React.FC = () => {
  const [versionInfo, setVersionInfo] = useState<VersionInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const fetchVersion = async () => {
      try {
        const response = await fetch('/api/version');
        if (!response.ok) {
          throw new Error('Failed to fetch version information');
        }
        const data = await response.json();
        setVersionInfo(data);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'An unknown error occurred');
      } finally {
        setLoading(false);
      }
    };

    fetchVersion();

    // Signal that app is ready (for tests)
    document.documentElement.setAttribute('data-app-ready', 'true');
    return () => {
      document.documentElement.removeAttribute('data-app-ready');
    };
  }, []);

  return (
    <div className="container mx-auto px-4 py-8 max-w-4xl">
      <div className="flex items-center gap-3 mb-8">
        <div className="bg-primary/10 p-2 rounded-lg">
          <Info className="w-8 h-8 text-primary" />
        </div>
        <div>
          <h1 className="text-3xl font-bold tracking-tight">About Family Assistant</h1>
          <p className="text-muted-foreground">Version and build information</p>
        </div>
      </div>

      <div className="grid gap-6">
        <Card>
          <CardHeader>
            <CardTitle>System Version</CardTitle>
            <CardDescription>Details about the currently running instance</CardDescription>
          </CardHeader>
          <CardContent>
            {loading ? (
              <div className="flex justify-center py-8">
                <p>Loading version information...</p>
              </div>
            ) : error ? (
              <div className="bg-destructive/10 text-destructive p-4 rounded-md">
                <p>Error: {error}</p>
              </div>
            ) : versionInfo ? (
              <div className="space-y-6">
                <div className="flex items-center justify-between border-b pb-4">
                  <div className="flex items-center gap-3">
                    <Package className="w-5 h-5 text-muted-foreground" />
                    <span className="font-medium">Application Version</span>
                  </div>
                  <Badge variant="outline" className="text-base px-3 py-1">
                    v{versionInfo.version}
                  </Badge>
                </div>

                <div className="flex items-center justify-between border-b pb-4">
                  <div className="flex items-center gap-3">
                    <GitBranch className="w-5 h-5 text-muted-foreground" />
                    <span className="font-medium">Git Commit</span>
                  </div>
                  <code className="bg-muted px-2 py-1 rounded text-sm">
                    {versionInfo.git_commit.substring(0, 8)}
                  </code>
                </div>

                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <Calendar className="w-5 h-5 text-muted-foreground" />
                    <span className="font-medium">Build Date / Tag</span>
                  </div>
                  <span className="text-muted-foreground">{versionInfo.build_date}</span>
                </div>
              </div>
            ) : null}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>About the Project</CardTitle>
          </CardHeader>
          <CardContent className="prose dark:prose-invert max-w-none">
            <p>
              Family Assistant is a comprehensive personal assistant platform designed to help
              manage daily tasks, documents, and notes using advanced AI technologies.
            </p>
            <p>
              Built with FastAPI, React, and integrated with various LLM providers, it provides a
              seamless experience across Telegram and Web interfaces.
            </p>
          </CardContent>
        </Card>
      </div>
    </div>
  );
};

export default AboutPage;
