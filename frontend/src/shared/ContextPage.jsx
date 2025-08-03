import React, { useState, useEffect } from 'react';

const ContextPage = () => {
  const [contextData, setContextData] = useState('Loading...');
  const [error, setError] = useState(null);

  useEffect(() => {
    const fetchContext = async () => {
      try {
        const response = await fetch('/api/v1/context');
        if (response.ok) {
          const text = await response.text();
          setContextData(text);
        } else {
          setError(`Failed to load context: ${response.status}`);
        }
      } catch (err) {
        setError(`Error loading context: ${err.message}`);
      }
    };

    fetchContext();
  }, []);

  return (
    <div className="context-page">
      <h1>Context Information</h1>
      {error ? (
        <div style={{ color: 'red' }}>
          <p>Error: {error}</p>
        </div>
      ) : (
        <pre style={{ whiteSpace: 'pre-wrap', fontFamily: 'monospace' }}>{contextData}</pre>
      )}
    </div>
  );
};

export default ContextPage;
