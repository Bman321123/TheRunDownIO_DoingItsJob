import "./globals.css";
import type { Metadata } from "next";
import { Geist } from "next/font/google";
import Script from "next/script";
import { cn } from "@/lib/utils";
import { AppHeader } from "@/components/AppHeader";
import { TooltipProvider } from "@/components/ui/tooltip";

const geist = Geist({subsets:['latin'],variable:'--font-sans'});

export const metadata: Metadata = {
  title: "Arb Dashboard",
  description: "Lightning-fast arbitrage dashboard",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={cn("font-sans", geist.variable, "dark")}>
      <body>
        <Script
          id="theme-init"
          strategy="beforeInteractive"
          dangerouslySetInnerHTML={{
            __html: `
          (function () {
            try {
              var t = localStorage.getItem('theme');
              if (t === 'light') document.documentElement.classList.remove('dark');
              else document.documentElement.classList.add('dark');
            } catch (e) {}
          })();
        `,
          }}
        />
        <TooltipProvider>
          <div className="min-h-screen">
            <AppHeader />
            <div className="mx-auto max-w-6xl px-4 py-6">{children}</div>
          </div>
        </TooltipProvider>
      </body>
    </html>
  );
}

