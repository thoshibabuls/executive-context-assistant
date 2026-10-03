import type { ReactNode } from "react";
import "./globals.css";

export const metadata = { title: "Executive Context Assistant" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <nav>
          <a href="/">Today</a>
          <a href="/tasks">Tasks</a>
          <a href="/people">People</a>
          <a href="/reminders">Reminders</a>
          <a href="/chat">Ask</a>
        </nav>
        <main>{children}</main>
      </body>
    </html>
  );
}
