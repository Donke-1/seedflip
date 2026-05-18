import path from "path"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// Built artifacts go into ../dist so the FastAPI backend (which mounts
// `dist/` as static files at /) picks them up without an extra copy step.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    outDir: path.resolve(__dirname, "../dist"),
    emptyOutDir: true,
  },
})

