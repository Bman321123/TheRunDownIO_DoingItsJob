import type { Config } from "tailwindcss";

export default {
    darkMode: ["class"],
    content: ["./src/**/*.{ts,tsx}"],
  theme: {
  	extend: {
  		colors: {
  			// Background layers (PRD)
  			base: "var(--color-base)",
  			surface: "var(--color-surface)",
  			raised: "var(--color-raised)",
  			overlay: "var(--color-overlay)",

  			// Shadcn-like tokens backed by CSS vars (used by ui/* components)
  			background: "var(--color-background)",
  			foreground: "var(--color-foreground)",
  			card: "var(--color-card)",
  			"card-foreground": "var(--color-card-foreground)",
  			popover: "var(--color-popover)",
  			"popover-foreground": "var(--color-popover-foreground)",
  			muted: "var(--color-muted)",
  			"muted-foreground": "var(--color-muted-foreground)",
  			accent: "var(--color-accent)",
  			"accent-foreground": "var(--color-accent-foreground)",
  			input: "var(--color-input)",
  			ring: "var(--color-ring)",

  			// Text + odds colors (PRD)
  			primary: "var(--color-text-primary)",
  			secondary: "var(--color-text-secondary)",
  			"muted-text": "var(--color-text-muted)",
  			"accent-green": "var(--color-accent-green)",
  			"accent-amber": "var(--color-accent-amber)",
  			"accent-red": "var(--color-accent-red)",

  			// Borders (PRD)
  			subtle: "var(--color-border-subtle)",
  			emphasis: "var(--color-border-emphasis)"
  		},
      borderColor: {
        border: "var(--color-border-subtle)",
      },
  		borderRadius: {
  			DEFAULT: '6px',
  			lg: '12px',
  			pill: '24px'
  		},
  		transitionDuration: {
  			DEFAULT: '150ms',
  			slow: '250ms'
  		},
  		boxShadow: {
  			card: '0 1px 3px rgba(0,0,0,0.6), 0 0 0 1px rgba(255,255,255,0.07)',
  			'card-hover': '0 4px 16px rgba(0,0,0,0.8), 0 0 0 1px rgba(255,255,255,0.12)',
  			'glow-green': '0 0 16px rgba(52,211,153,0.25)',
  			'glow-scan': '0 0 24px rgba(52,211,153,0.4)',
  		},
  		keyframes: {
  			'accordion-down': {
  				from: {
  					height: '0'
  				},
  				to: {
  					height: 'var(--radix-accordion-content-height)'
  				}
  			},
  			'accordion-up': {
  				from: {
  					height: 'var(--radix-accordion-content-height)'
  				},
  				to: {
  					height: '0'
  				}
  			}
  		},
  		animation: {
  			'accordion-down': 'accordion-down 0.2s ease-out',
  			'accordion-up': 'accordion-up 0.2s ease-out'
  		}
  	}
  },
  plugins: [],
} satisfies Config;

