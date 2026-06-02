import type { NextConfig } from "next";

// REST API is proxied through the Next server so the browser only ever talks to
// the web origin (:3000). This removes the dependency on the API port (:8000)
// being directly reachable from the client — critical for Tailscale/SSH access
// where only :3000 may be exposed — and sidesteps CORS entirely (same-origin).
// web runs network_mode: host, so 127.0.0.1:8000 reaches the api process.
const API_PROXY_TARGET = process.env.API_PROXY_TARGET || "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  output: "standalone",
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${API_PROXY_TARGET}/api/:path*` },
    ];
  },
};

export default nextConfig;
