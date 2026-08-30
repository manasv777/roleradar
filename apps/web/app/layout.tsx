import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'roleradar',
  description: 'Scout internship and new-grad roles in the fields you pick.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
