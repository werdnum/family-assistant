import React, { useState, useEffect } from 'react';
import { MarkdownText } from '../chat/MarkdownText';
import styles from './ContextPage.module.css';

interface ProcessingProfile {
  id: string;
  description: string;
  llm_model: string;
  provider: string;
  tools_count: number;
  context_providers: string[];
}

interface ContextProvider {
  provider_name: string;
  fragments: string[];
  error: string | null;
  fragment_count: number;
}

interface ContextData {
  profile_id: string;
  aggregated_context: string;
  context_providers: ContextProvider[];
  total_fragments: number;
  providers_with_errors: string[];
  system_prompt_template: string;
  formatted_system_prompt: string;
}

const ContextPage: React.FC = () => {
  const [profiles, setProfiles] = useState<ProcessingProfile[]>([]);
  const [selectedProfileId, setSelectedProfileId] = useState<string>('');
  const [contextData, setContextData] = useState<ContextData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedSections, setExpandedSections] = useState<Set<string>>(new Set());

  // Fetch available profiles
  useEffect(() => {
    const fetchProfiles = async () => {
      try {
        const response = await fetch('/api/v1/context/profiles');
        if (response.ok) {
          const profilesData = await response.json();
          setProfiles(profilesData);
          // Set default profile if none selected
          if (profilesData.length > 0 && !selectedProfileId) {
            setSelectedProfileId(profilesData[0].id);
          }
        } else {
          setError(`Failed to load profiles: ${response.status}`);
        }
      } catch (err) {
        setError(`Error loading profiles: ${(err as Error).message}`);
      }
    };

    fetchProfiles();
  }, []);

  // Fetch context data for selected profile
  useEffect(() => {
    if (!selectedProfileId) {
      return;
    }

    const fetchContext = async () => {
      setLoading(true);
      setError(null);
      try {
        const url = selectedProfileId
          ? `/api/v1/context?profile_id=${encodeURIComponent(selectedProfileId)}`
          : '/api/v1/context';

        const response = await fetch(url);
        if (response.ok) {
          const data = await response.json();
          setContextData(data);
          // Only expand the formatted system prompt by default
          setExpandedSections(new Set(['formatted-system-prompt']));
        } else {
          setError(`Failed to load context: ${response.status}`);
        }
      } catch (err) {
        setError(`Error loading context: ${(err as Error).message}`);
      } finally {
        setLoading(false);
      }
    };

    fetchContext();
  }, [selectedProfileId]);

  // Set page title and coordinate data-app-ready with loading state
  useEffect(() => {
    document.title = 'Context - Family Assistant';

    if (!loading) {
      document.getElementById('app-root')?.setAttribute('data-app-ready', 'true');
    } else {
      document.getElementById('app-root')?.removeAttribute('data-app-ready');
    }

    return () => {
      document.getElementById('app-root')?.removeAttribute('data-app-ready');
    };
  }, [loading]);

  const toggleSection = (sectionName: string) => {
    const newExpanded = new Set(expandedSections);
    if (newExpanded.has(sectionName)) {
      newExpanded.delete(sectionName);
    } else {
      newExpanded.add(sectionName);
    }
    setExpandedSections(newExpanded);
  };

  const selectedProfile = profiles.find((p) => p.id === selectedProfileId);

  return (
    <div className={styles['context-page']}>
      <h1>Context Information</h1>

      {/* Profile Selector */}
      <div className={styles['profile-selector']}>
        <label htmlFor="profile-select">Processing Profile:</label>
        <select
          id="profile-select"
          value={selectedProfileId}
          onChange={(e) => setSelectedProfileId(e.target.value)}
          className={styles['profile-select']}
        >
          {profiles.map((profile) => (
            <option key={profile.id} value={profile.id}>
              {profile.id} - {profile.description}
            </option>
          ))}
        </select>
      </div>

      {error && (
        <div className={styles['error-message']}>
          <p>Error: {error}</p>
        </div>
      )}

      {loading && <div className={styles.loading}>Loading context data...</div>}

      {contextData && selectedProfile && (
        <div className={styles['context-content']}>
          {/* Profile Information Section */}
          <div className={styles['context-section']}>
            <button
              className={styles['section-header']}
              onClick={() => toggleSection('profile-info')}
            >
              <span className={styles['toggle-icon']}>
                {expandedSections.has('profile-info') ? '▼' : '▶'}
              </span>
              Profile Information
            </button>
            {expandedSections.has('profile-info') && (
              <div className={styles['section-content']}>
                <div className={styles['profile-info']}>
                  <p>
                    <strong>ID:</strong> {selectedProfile.id}
                  </p>
                  <p>
                    <strong>Description:</strong> {selectedProfile.description}
                  </p>
                  <p>
                    <strong>LLM Model:</strong> {selectedProfile.llm_model}
                  </p>
                  <p>
                    <strong>Provider:</strong> {selectedProfile.provider}
                  </p>
                  <p>
                    <strong>Available Tools:</strong> {selectedProfile.tools_count}
                  </p>
                  <p>
                    <strong>Context Providers:</strong>{' '}
                    {selectedProfile.context_providers.join(', ')}
                  </p>
                  <p>
                    <strong>Total Context Fragments:</strong> {contextData.total_fragments}
                  </p>
                  {contextData.providers_with_errors.length > 0 && (
                    <p className={styles['error-info']}>
                      <strong>Providers with Errors:</strong>{' '}
                      {contextData.providers_with_errors.join(', ')}
                    </p>
                  )}
                </div>
              </div>
            )}
          </div>

          {/* Formatted System Prompt Section */}
          <div className={styles['context-section']}>
            <button
              className={styles['section-header']}
              onClick={() => toggleSection('formatted-system-prompt')}
            >
              <span className={styles['toggle-icon']}>
                {expandedSections.has('formatted-system-prompt') ? '▼' : '▶'}
              </span>
              System Prompt (Formatted)
            </button>
            {expandedSections.has('formatted-system-prompt') && (
              <div className={styles['section-content']}>
                <div className={styles['system-prompt']}>
                  <MarkdownText text={contextData.formatted_system_prompt} />
                </div>
              </div>
            )}
          </div>

          {/* System Prompt Template Section */}
          <div className={styles['context-section']}>
            <button
              className={styles['section-header']}
              onClick={() => toggleSection('system-prompt')}
            >
              <span className={styles['toggle-icon']}>
                {expandedSections.has('system-prompt') ? '▼' : '▶'}
              </span>
              System Prompt Template (Raw)
            </button>
            {expandedSections.has('system-prompt') && (
              <div className={styles['section-content']}>
                <div className={styles['system-prompt']}>
                  <code className={styles['system-prompt-code']}>
                    {contextData.system_prompt_template}
                  </code>
                </div>
              </div>
            )}
          </div>

          {/* Aggregated Context Section */}
          <div className={styles['context-section']}>
            <button
              className={styles['section-header']}
              onClick={() => toggleSection('aggregated-context')}
            >
              <span className={styles['toggle-icon']}>
                {expandedSections.has('aggregated-context') ? '▼' : '▶'}
              </span>
              Aggregated Context
            </button>
            {expandedSections.has('aggregated-context') && (
              <div className={styles['section-content']}>
                <div className={styles['aggregated-context']}>
                  {contextData.aggregated_context ? (
                    <MarkdownText text={contextData.aggregated_context} />
                  ) : (
                    <p className={styles['no-context']}>No aggregated context available</p>
                  )}
                </div>
              </div>
            )}
          </div>

          {/* Context Providers Sections */}
          {contextData.context_providers.map((provider) => (
            <div key={provider.provider_name} className={styles['context-section']}>
              <button
                className={styles['section-header']}
                onClick={() => toggleSection(provider.provider_name)}
              >
                <span className={styles['toggle-icon']}>
                  {expandedSections.has(provider.provider_name) ? '▼' : '▶'}
                </span>
                {provider.provider_name}
                <span className={styles['fragment-count']}>
                  ({provider.fragment_count} fragment{provider.fragment_count !== 1 ? 's' : ''})
                </span>
                {provider.error && <span className={styles['error-indicator']}>⚠️</span>}
              </button>
              {expandedSections.has(provider.provider_name) && (
                <div className={styles['section-content']}>
                  {provider.error ? (
                    <div className={styles['provider-error']}>
                      <p>
                        <strong>Error:</strong> {provider.error}
                      </p>
                    </div>
                  ) : provider.fragments.length > 0 ? (
                    <div className={styles['provider-fragments']}>
                      {provider.fragments.map((fragment, index) => (
                        <div key={index} className={styles.fragment}>
                          <MarkdownText text={fragment} />
                          {index < provider.fragments.length - 1 && (
                            <hr className={styles['fragment-separator']} />
                          )}
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className={styles['no-fragments']}>No context fragments available</p>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

export default ContextPage;
