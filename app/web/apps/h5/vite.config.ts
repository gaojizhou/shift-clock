import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "/h5/",
  server: { port: 5174, proxy: { "/api": "http://127.0.0.1:3000", "/h5/test": "http://127.0.0.1:3000" } },
});
