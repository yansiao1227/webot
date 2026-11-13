import React from 'react';
import { createRoot } from 'react-dom/client';
import { HeadlessMantineProvider } from '@mantine/core';
import { App } from './App';
import './style.css';

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <HeadlessMantineProvider>
      <App />
    </HeadlessMantineProvider>
  </React.StrictMode>,
);
