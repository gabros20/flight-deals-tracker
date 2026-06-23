---
name: flight-deals-creative-usage
description: Creative, flexible, and anti-stuck strategies for using the flight-deals CLI tool. Covers flag combinations, workflows, and adaptive search tactics for Ryanair/Wizz deals.
category: travel
version: 1.0
---

# Flight Deals Tracker - Creative Usage Skill

This skill teaches the agent how to use the `flight-deals` tool in powerful, creative ways without getting stuck on no results.

## Core Commands & Flags
See the full guide at docs/FLIGHT-DEALS-CREATIVE-USAGE-SKILL.md for complete details.

**Primary command**: `search` with these high-leverage flags:
- `--category` (european-islands, seaside, italian-gems, shopping)
- `--connections` (unlocks 1-stops + self-transfers)
- Date windows: `--date-from`/`--date-to` + `--return-from`/`--return-to`
- `--fresh` (bypass cache)
- `--max-price`, `--history-window`, `--ground-prefer`, `--sort-by`

## Key Creative Strategies

### 1. Broad → Narrow Protocol
Always run wide date windows + category first, then refine.

### 2. Direct vs Connections Toggle
- No `--connections`: Fast local Ryanair/Wizz directs (farfnd)
- With `--connections`: Apify + ground transport for other airlines and stops

### 3. Anti-Stuck Decision Tree
If zero results:
1. Widen date windows
2. Add `--connections`
3. Use `--fresh`
4. Try different category
5. Run `collect` first then search with `--history-window`

### 4. History Power User Workflow
collect → history-stats → targeted search with history comparison

### 5. Flag Combinations to Try
- Basic getaway: category + date windows + return windows
- Connections hunt: add `--connections --max-ground-minutes 240`
- Price drop focus: `--history-window 30` + prior `collect`
- Efficiency: `--sort-by efficiency --ground-prefer any`

## Recommended Agent Behavior
When user asks for deals:
- Propose at least 2-3 different flag/strategy combinations
- Suggest running both with and without `--connections`
- Recommend `collect` before important searches
- Always enforce the numbered list + emoji + links format in outputs
- Reference specific categories and example date windows

## Quick Start Examples
See the full markdown guide for dozens of ready-to-run command patterns.

This skill should be loaded whenever the user discusses flight searches, tracking, or deals from BUD.
