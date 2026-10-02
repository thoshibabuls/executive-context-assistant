// The browser talks to the API same-origin through this rewrite (BACKEND_DESIGN.md §16.1), so
// the session cookie stays first-party and SameSite=Lax holds.
const apiOrigin = process.env.ECA_API_ORIGIN ?? "http://localhost:8000";

/** @type {import('next').NextConfig} */
const nextConfig = {
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiOrigin}/api/:path*` }];
  },
};

export default nextConfig;
