"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Settings2Icon, MenuIcon } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Sheet, SheetContent, SheetTrigger } from "@/components/ui/sheet";

const NAV_ITEMS = [
  { href: "/", label: "Dashboard" },
];

export function AppHeader() {
  const pathname = usePathname();
  const [theme, setTheme] = useState<"dark" | "light">("dark");

  useEffect(() => {
    try {
      const stored = localStorage.getItem("theme");
      setTheme(stored === "light" ? "light" : "dark");
    } catch {
      setTheme("dark");
    }
  }, []);

  useEffect(() => {
    if (typeof document === "undefined") return;
    const root = document.documentElement;
    if (theme === "dark") root.classList.add("dark");
    else root.classList.remove("dark");
    try {
      localStorage.setItem("theme", theme);
    } catch {
      // ignore
    }
  }, [theme]);

  const ThemeSwitch = (
    <div className="flex items-center justify-between gap-3">
      <div className="min-w-0">
        <div className="text-sm font-semibold leading-tight text-primary">Theme</div>
        <div className="text-xs leading-tight text-secondary">
          {theme === "dark" ? "Dark mode enabled" : "Light mode enabled"}
        </div>
      </div>
      <Switch
        checked={theme === "dark"}
        onCheckedChange={(checked) => setTheme(checked ? "dark" : "light")}
        aria-label="Toggle dark mode"
      />
    </div>
  );

  return (
    <header className="h-14 border-b border-border bg-raised">
      <div className="mx-auto flex h-full max-w-6xl items-center justify-between px-4">
        <div className="flex items-center gap-6">
          <Link href="/" className="text-sm font-semibold tracking-tight text-primary">
            Arb Dashboard
          </Link>

          <nav className="hidden items-center gap-4 md:flex">
            {NAV_ITEMS.map((item) => {
              const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={[
                    "text-sm font-medium transition-colors duration-150",
                    active ? "text-primary" : "text-secondary hover:text-primary",
                  ].join(" ")}
                >
                  <span className="relative pb-1">
                    {item.label}
                    {active ? (
                      <span className="absolute left-0 right-0 -bottom-1 h-[1.5px] bg-primary" />
                    ) : null}
                  </span>
                </Link>
              );
            })}
          </nav>
        </div>

        <div className="hidden items-center gap-2 md:flex">
          <Link
            href="https://21st.dev/community/components"
            className="text-sm text-secondary hover:text-primary"
          >
            Components
          </Link>
          <Button
            asChild
            variant="primary"
            className="ml-2 bg-accent-green text-[#0A0A0F] hover:bg-accent-green/90"
          >
            <a href="http://127.0.0.1:8888/arbs" target="_blank" rel="noreferrer">
              API
            </a>
          </Button>

          <Sheet>
            <SheetTrigger asChild>
              <Button
                variant="primary"
                size="icon"
                aria-label="Open settings menu"
                className="ml-2 bg-accent-green text-[#0A0A0F] hover:bg-accent-green/90"
              >
                <Settings2Icon className="h-4 w-4" />
              </Button>
            </SheetTrigger>
            <SheetContent side="right" className="w-[80vw] sm:w-80">
              <div className="mt-6 space-y-6">
                <div className="space-y-1.5">
                  <div className="flex items-center gap-2">
                    <Settings2Icon className="h-4 w-4" />
                    <div className="text-sm font-semibold">Settings</div>
                  </div>
                  <div className="text-xs text-secondary">Appearance controls.</div>
                </div>
                {ThemeSwitch}
              </div>
            </SheetContent>
          </Sheet>
        </div>

        <div className="md:hidden">
          <Sheet>
            <SheetTrigger asChild>
              <Button variant="secondary" size="icon" aria-label="Open navigation">
                <MenuIcon className="h-4 w-4" />
              </Button>
            </SheetTrigger>
            <SheetContent side="right" className="w-[80vw] sm:w-80">
              <div className="mt-6 flex flex-col gap-4">
                {NAV_ITEMS.map((item) => (
                  <Link key={item.href} href={item.href} className="text-sm font-medium text-primary">
                    {item.label}
                  </Link>
                ))}
                <div className="pt-2">
                  <Button
                    asChild
                    variant="primary"
                    className="w-full bg-accent-green text-[#0A0A0F] hover:bg-accent-green/90"
                  >
                    <a href="http://127.0.0.1:8888/arbs" target="_blank" rel="noreferrer">
                      API
                    </a>
                  </Button>
                </div>

                <div className="pt-2 border-t border-border" />
                <div>{ThemeSwitch}</div>
              </div>
            </SheetContent>
          </Sheet>
        </div>
      </div>
    </header>
  );
}

