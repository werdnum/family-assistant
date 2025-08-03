import React from 'react';
import { Link, useLocation } from 'react-router-dom';

const Layout = ({ children }) => {
  const location = useLocation();

  // Extract current page from pathname
  const currentPage = location.pathname.split('/')[1] || 'home';

  return (
    <>
      <header>
        <nav>
          {/* Assistant Data */}
          <div className="nav-group">
            <span className="nav-label">Data</span>
            <a href="/notes">Notes</a>
            <Link to="/context">Context</Link>
          </div>
          <span className="nav-separator">|</span>

          {/* Documents */}
          <div className="nav-group">
            <span className="nav-label">Documents</span>
            <a href="/documents/">List</a>
            <a href="/documents/upload">Upload</a>
            <a href="/vector-search">Search</a>
          </div>
          <span className="nav-separator">|</span>

          {/* Chat & History */}
          <Link to="/chat" className={currentPage === 'chat' ? 'current-page' : ''}>
            Chat
          </Link>
          <a href="/history">History</a>
          <span className="nav-separator">|</span>

          {/* Automation */}
          <div className="nav-group">
            <span className="nav-label">Automation</span>
            <a href="/events">Events</a>
            <a href="/event-listeners">Event Listeners</a>
          </div>
          <span className="nav-separator">|</span>

          {/* Internal/Admin */}
          <div className="nav-group">
            <span className="nav-label">Internal</span>
            <Link to="/tools" className={currentPage === 'tools' ? 'current-page' : ''}>
              Tools
            </Link>
            <a href="/tasks">Task Queue</a>
            <Link to="/errors" className={currentPage === 'errors' ? 'current-page' : ''}>
              Error Logs
            </Link>
          </div>
          <span className="nav-separator">|</span>

          {/* Help */}
          <a href="/docs/">Help</a>
        </nav>
      </header>

      <main>{children}</main>

      <footer>
        <p>&copy; {new Date().getFullYear()} Family Assistant</p>
      </footer>
    </>
  );
};

export default Layout;
