import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "/",
  // /h5/test 透传给后端：本地身份启动器经此打 /h5/test/auth 登录后 302 回 admin（cookie 落 :5173 同源）。
  server: { port: 5173, proxy: { "/api": "http://127.0.0.1:3000", "/h5/test": "http://127.0.0.1:3000" } },
});
