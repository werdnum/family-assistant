import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import Layout from '../shared/Layout';
import NotesApp from './NotesApp';

// Import styles
import 'simpledotcss/simple.css';
import '../custom.css';

// Mount the notes app
function mountNotesApp() {
  const container = document.getElementById('notes-root');
  if (container) {
    const root = ReactDOM.createRoot(container);
    root.render(
      <React.StrictMode>
        <BrowserRouter>
          <Layout>
            <NotesApp />
          </Layout>
        </BrowserRouter>
      </React.StrictMode>
    );
  }
}

// Mount when DOM is ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', mountNotesApp);
} else {
  mountNotesApp();
}
