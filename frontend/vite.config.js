import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],

  // Everything under /api goes to the FastAPI process. With this, the browser
  // only ever talks to one origin during development, so CORS is a deployment
  // concern instead of something to debug on day one — and no VITE_API_URL is
  // needed locally.
  //
  // 127.0.0.1 rather than localhost on purpose: Node resolves localhost to ::1
  // first on Windows, uvicorn binds 127.0.0.1 by default, and the result is a
  // proxy that refuses every request while both processes look healthy.
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
})
