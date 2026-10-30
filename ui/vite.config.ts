import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The console talks to the ops API directly (CORS allows localhost:5173); no proxy needed.
export default defineConfig({
  plugins: [react()],
  server: { host: "127.0.0.1", port: 5173, strictPort: true },
});
