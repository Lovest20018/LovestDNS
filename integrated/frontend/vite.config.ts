import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const apiProxy = {
  target: process.env.SUITE_DEV_API_URL || 'http://127.0.0.1:8080',
  changeOrigin: false, // Flask compares the browser's Origin with the forwarded Host.
};

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': apiProxy,
      '/auth': apiProxy,
    },
  },
});
