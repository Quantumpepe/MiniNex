import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App.jsx';
import './styles.css';
import { PrivyProvider } from '@privy-io/react-auth';
import { PRIVY_APP_ID, PRIVY_PROVIDER_CONFIG } from './privyConfig.js';

// The public app ID enables wallet login. Never put a Privy app secret or
// backend authorization key inside an Android/iOS bundle.
createRoot(document.getElementById('root')).render(
  PRIVY_APP_ID
    ? <PrivyProvider appId={PRIVY_APP_ID} config={PRIVY_PROVIDER_CONFIG}><App /></PrivyProvider>
    : <App />
);
