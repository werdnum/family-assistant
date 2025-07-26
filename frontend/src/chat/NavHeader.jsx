import React from 'react';

const NavHeader = () => {
  return (
    <header>
      <h1>Chat</h1>
      <nav>
        {/* Assistant Data */}
        <div className="nav-group">
          <span className="nav-label">Data</span>
          <a href="/">Notes</a>
          <a href="/context">Context</a>
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
        <a href="/chat" className="current-page">Chat</a>
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
          <a href="/tools">Tools</a>
          <a href="/tasks">Task Queue</a>
          <a href="/errors/">Error Logs</a>
        </div>
        <span className="nav-separator">|</span>
        
        {/* Help */}
        <a href="/docs/">Help</a>
      </nav>
    </header>
  );
};

export default NavHeader;
