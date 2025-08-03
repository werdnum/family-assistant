import React, { useState, useEffect } from 'react';
import { useParams, Link } from 'react-router-dom';

const ErrorDetail = () => {
  const { errorId } = useParams();
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState(null);

  useEffect(() => {
    const fetchErrorDetail = async () => {
      setLoading(true);
      setFetchError(null);

      try {
        const response = await fetch(`/api/errors/${errorId}`);
        if (!response.ok) {
          if (response.status === 404) {
            throw new Error('Error log not found');
          }
          throw new Error(`Failed to fetch error details: ${response.statusText}`);
        }

        const data = await response.json();
        setError(data);
      } catch (err) {
        setFetchError(err.message);
      } finally {
        setLoading(false);
      }
    };

    if (errorId) {
      fetchErrorDetail();
    }
  }, [errorId]);

  const getLevelBadgeClass = (level) => {
    switch (level) {
      case 'CRITICAL':
        return 'badge bg-danger me-2';
      case 'ERROR':
        return 'badge bg-warning text-dark me-2';
      case 'WARNING':
        return 'badge bg-warning text-dark me-2';
      default:
        return 'badge bg-secondary me-2';
    }
  };

  const formatTimestamp = (timestamp) => {
    const date = new Date(timestamp);
    return date.toLocaleString('en-US', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      fractionalSecondDigits: 3,
      hour12: false,
    });
  };

  if (loading) {
    return <div className="loading">Loading error details...</div>;
  }

  if (fetchError) {
    return (
      <div className="error-detail">
        <div className="error-detail-header">
          <h1>Error Details</h1>
          <Link to="/errors" className="btn-secondary">
            Back to List
          </Link>
        </div>
        <div className="error-message">
          <strong>Error loading data:</strong> {fetchError}
        </div>
      </div>
    );
  }

  if (!error) {
    return (
      <div className="error-detail">
        <div className="error-detail-header">
          <h1>Error Details</h1>
          <Link to="/errors" className="btn-secondary">
            Back to List
          </Link>
        </div>
        <div className="alert alert-info">Error not found.</div>
      </div>
    );
  }

  return (
    <div className="error-detail">
      <div className="error-detail-header">
        <h1>Error Details</h1>
        <Link to="/errors" className="btn-secondary">
          Back to List
        </Link>
      </div>

      <div className="card">
        <div className="card-header">
          <span className={getLevelBadgeClass(error.level)}>{error.level}</span>
          Error #{error.id}
        </div>
        <div className="card-body">
          <div className="row mb-3">
            <div className="col-md-6">
              <h6>Timestamp</h6>
              <p>{formatTimestamp(error.timestamp)}</p>
            </div>
            <div className="col-md-6">
              <h6>Logger</h6>
              <p>
                <code>{error.logger_name}</code>
              </p>
            </div>
          </div>

          <div className="row mb-3">
            <div className="col-md-6">
              <h6>Module</h6>
              <p>
                <code>{error.module || 'N/A'}</code>
              </p>
            </div>
            <div className="col-md-6">
              <h6>Function</h6>
              <p>
                <code>{error.function_name || 'N/A'}</code>
              </p>
            </div>
          </div>

          <div className="mb-3">
            <h6>Message</h6>
            <div className="alert alert-light">
              <pre className="mb-0" style={{ whiteSpace: 'pre-wrap' }}>
                {error.message}
              </pre>
            </div>
          </div>

          {error.exception_type && (
            <div className="mb-3">
              <h6>Exception</h6>
              <div className="alert alert-danger">
                <strong>{error.exception_type}</strong>: {error.exception_message}
              </div>
            </div>
          )}

          {error.traceback && (
            <div className="mb-3">
              <h6>Traceback</h6>
              <div className="bg-dark text-light p-3 rounded" style={{ overflowX: 'auto' }}>
                <pre className="mb-0">
                  <code>{error.traceback}</code>
                </pre>
              </div>
            </div>
          )}

          {error.extra_data && (
            <div className="mb-3">
              <h6>Additional Data</h6>
              <div className="bg-light p-3 rounded">
                <pre className="mb-0">
                  <code>{JSON.stringify(error.extra_data, null, 2)}</code>
                </pre>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export default ErrorDetail;
