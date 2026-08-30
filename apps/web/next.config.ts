import type { NextConfig } from 'next';

const config: NextConfig = {
  // The API runs as a separate process; proxying keeps the browser on one
  // origin so there is no CORS surprise during local development.
  async rewrites() {
    const api = process.env.ROLERADAR_API ?? 'http://127.0.0.1:8100';
    return [{ source: '/api/:path*', destination: `${api}/api/:path*` }];
  },
};

export default config;
