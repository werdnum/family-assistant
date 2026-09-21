import React, { useEffect, useState } from 'react';
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
  include_aggregated_context: boolean;
  context_providers: ContextProvider[];
  total_fragments: number;
  providers_with_errors: string[];
  system_prompt_template: string;
  formatted_system_prompt: string;
  turn_context_block: string;
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
          // Expand the two halves of what the model actually receives by default
          setExpandedSections(new Set(['formatted-system-prompt', 'turn-context']));
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
                  <p>
                    <strong>Receives Aggregated Context:</strong>{' '}
                    {contextData.include_aggregated_context
                      ? 'Yes'
                      : 'No (include_aggregated_context is off)'}
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

          {/* Turn Context Block Section */}
          <div className={styles['context-section']}>
            <button
              className={styles['section-header']}
              onClick={() => toggleSection('turn-context')}
            >
              <span className={styles['toggle-icon']}>
                {expandedSections.has('turn-context') ? '▼' : '▶'}
              </span>
              Turn Context Block (Appended to End of Conversation)
            </button>
            {expandedSections.has('turn-context') && (
              <div className={styles['section-content']}>
                <div className={styles['provider-fragments']}>
                  <p className={styles['no-context']}>
                    Sent as a user message at the <strong>end</strong> of the conversation on every
                    request — it is not part of the system prompt. It always carries the current
                    time, and carries the aggregated context only when the profile opts in with
                    include_aggregated_context.
                  </p>
                  {!contextData.include_aggregated_context && (
                    <div className={styles['provider-error']}>
                      <p>
                        <strong>This profile does not receive the aggregated context.</strong>{' '}
                        include_aggregated_context is off for {contextData.profile_id}, so the
                        notes, calendar and other provider context shown below are <em>not</em> sent
                        to it. Only the current time is.
                      </p>
                    </div>
                  )}
                  <div className={styles['system-prompt']}>
                    <code className={styles['system-prompt-code']}>
                      {contextData.turn_context_block}
                    </code>
                  </div>
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
              {!contextData.include_aggregated_context && (
                <span className={styles['fragment-count']}>(not sent to this profile)</span>
              )}
            </button>
            {expandedSections.has('aggregated-context') && (
              <div className={styles['section-content']}>
                <div className={styles['provider-fragments']}>
                  {!contextData.include_aggregated_context && (
                    <div className={styles['provider-error']}>
                      <p>
                        Not sent to {contextData.profile_id} — this is what the context providers
                        produced, but the profile has include_aggregated_context off, so none of it
                        reaches the model.
                      </p>
                    </div>
                  )}
                  <div className={styles['aggregated-context']}>
                    {contextData.aggregated_context ? (
                      <MarkdownText text={contextData.aggregated_context} />
                    ) : (
                      <p className={styles['no-context']}>No aggregated context available</p>
                    )}
                  </div>
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
