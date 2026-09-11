---
version: alpha
name: Family Assistant
colors:
  background: '#ffffff'
  foreground: '#020817'
  primary: '#0f172a'
  primary-foreground: '#f8fafc'
  secondary: '#f1f5f9'
  secondary-foreground: '#020817'
  muted: '#f1f5f9'
  muted-foreground: '#64748b'
  accent: '#1e293b'
  accent-foreground: '#020817'
  destructive: '#ef4444'
  destructive-foreground: '#f8fafc'
  border: '#e2e8f0'
  ring: '#020817'
---

# Family Assistant Design System

## Overview

Family Assistant provides a clean, modern, and accessible user interface for managing family
information, tasks, and communications. The design is utilitarian yet friendly, relying on
high-contrast elements for clarity and soft colors to reduce visual fatigue. The styling is managed
primarily via Tailwind CSS, using a carefully selected palette of functional semantic colors.

## Colors

The color palette is built on high-contrast functional roles, using HSL-based tokens in the
underlying CSS that translate to the following HEX values:

- **Background (#ffffff):** Pure white to maximize readability for main content areas.
- **Foreground (#020817):** Deepest navy, almost black, used for primary text to ensure strict
  contrast.
- **Primary (#0f172a):** A very dark slate used for key actions, highlighted states, and important
  buttons.
- **Secondary (#f1f5f9):** A soft, light blue-gray used for secondary actions, interactive
  backgrounds, and lower-emphasis components.
- **Muted (#f1f5f9):** A light neutral used for disabled states, code blocks, and subdued
  backgrounds. Muted text is represented by a deeper slate (#64748b).
- **Accent (#1e293b):** A dark slate used for active states or elevated interactive elements.
- **Destructive (#ef4444):** A bright red reserved exclusively for destructive actions like deleting
  content.
- **Border (#e2e8f0):** A soft gray used to subtly define boundaries between elements without
  distracting the user.
