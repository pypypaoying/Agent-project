import React from 'react';
import ReactDOM from 'react-dom/client';
import { App } from './App';
import { initializeToken } from './api';
import './styles.css';

const authenticated = initializeToken();
ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode><App authenticated={authenticated} /></React.StrictMode>,
);
