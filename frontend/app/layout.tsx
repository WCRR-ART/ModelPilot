import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ModelPilot Dashboard",
  description: "Operational overview for the ModelPilot LLM gateway",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
