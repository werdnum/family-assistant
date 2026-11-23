# Browser Task Mini-Evaluation: Model, Effort, and Visual vs DOM Comparison

**Run date:** 2026-11-23\
**Scope:** 20 stratified live read-only tasks from historical `browser_profile` traffic\
**Total trials:** 160 DOM trials (8 model/effort configs at 210s / 30 steps) + 15 production-runway
rescue trials (480s / 50 steps) + 16 visual trials (8 cases × 2 visual configs at 480s / 60 steps)\
**Judge:** Blind side-by-side comparative consensus judging with `claude-opus-5-5` (randomized
per-case labels)\
**Result:** `gpt-6.2-sol` (`medium`) wins on both quality and cost-efficiency at full production
runway (**80% pass**, **4.05/5**, **$0.124/task**); `gemini-4-hydrogen` (`medium`) is a close second
(**75% pass**, **4.00/5**, **$0.214/task**); `gemini-4-neon` adds no lift over either; and
`visual-hydrogen-medium` strongly outperforms shipped `visual-flash-3.8` on
`browser_visual_profile`.

> All raw task prompts, historical answers, full transcripts, and case-level notes remain in the
> local gitignored `.review-eval-local/` tree. This document contains only sanitized aggregate
> findings.

## Executive Summary

1. **Keep `gpt-6.2-sol` (`reasoning_effort: medium`) as the `browser` tier primary.** At
   production's 50-step / 480s runway, it achieved the highest pass rate (**16/20, 80%**), highest
   mean score (**4.05/5**), **100% grounding**, 60.0s median latency, and best value
   (**$0.124/task**, **6.4 passes/$**). Raising the step cap from 30 to 50 rescued 4 of its 6
   earlier failures.
2. **`gemini-4-hydrogen` (`thinking_level: medium`) is the strongest alternative and a better
   fallback than `claude-sonnet-5-5`.** At 480s / 50 steps, `hydrogen-medium` reached **15/20
   (75%)** and **4.00/5** (9W / 10T / 1L over `sonnet-5.5`), at 60.5s median and **$0.214/task**.
   Lowering Hydrogen's thinking from default `high` to `medium` nearly halved median latency (110.8s
   → 60.5s) and cut cost by ~34% while improving mean score.
3. **`gemini-4-neon` offers no lift for browser tasks.** At default `high` thinking, Neon timed out
   on 9/20 tasks (40% pass, 196.5s median, $0.361/task). Even at `medium` effort and 480s runway,
   `neon-medium` trailed both `gpt-6.2-sol` and `hydrogen-medium` (**14/20, 70%**, 150.8s median,
   **$0.291/task** — 2.3× Sol's cost and 2.5× its latency).
4. **DOM (`browser_profile`) decisively beats visual (`browser_visual_profile`) as the default** —
   3–12× faster and 2–3× cheaper at equal-or-better quality, with 90–95% prompt caching vs 74–76% on
   visual.
5. **Upgrade `browser_visual_profile` from `gemini-3.8-flash` to `gemini-4-hydrogen` (`medium`).**
   Across 8 visual cases at 480s / 60 steps, `visual-hydrogen-medium` completed **7/8 vs 4/8**,
   raised mean score from **2.38 → 3.12 (+0.75)**, cut median latency by 35% (329s → 215s), and cut
   tool calls by 27% (31.5 → 22.9).

## Methodology

- **Harness:** `family_assistant.eval.browser` (`scripts/browser_eval.py`) runs the shipped
  `browser_profile` and `browser_visual_profile` system prompts and real local Playwright tools
  (`browser_dom` / `computer_use`) via `generate_response_stream()` with
  `SystemMessage.stable_prefix_len` so prompt caching matches production. Each candidate is a
  single-entry client with no fallback.
- **Task stratification (20 live read-only cases):** 5 single-page extractions, 3 multi-site price
  comparisons, 2 retail searches, 2 interactive no-submit quotes, 1 stateful store selector, 1
  paginated table extraction, 1 multi-page navigation, 1 long-page summary, 1 file-download /
  login-wall case, 1 social video transcript, 1 travel availability SPA, and 1 visual content case.
  Private event pages, expired tracking numbers, follow-ups, stale past-date tasks, and Turnstile
  probes were excluded.
- **Blind consensus judging:** Candidates ran concurrently per case to avoid live-page drift. Labels
  were deterministically shuffled per case (`A`, `B`, …) before passing replies, tool traces, and
  historical reference answers (explicitly marked as potentially stale for live prices/content) to
  `claude-opus-5-5` for structured 0–5 scoring and consensus classification (`pass` = score ≥ 4).

## Results: Production Runway (480s / 50 Steps, Top 5 DOM Contenders)

Trials that hit the initial 210s / 30-step cap were re-run at 480s / 50 steps and re-judged blindly
with `claude-opus-5-5`.

| Candidate         | Model                     |     Pass (≥4/5) | Mean Score | Grounded | Win/Tie | Consensus | Completed | Med. Latency | Avg Calls | Cache Hit | Cost/Task | Total Cost | Passes/$ |
| ----------------- | ------------------------- | --------------: | ---------: | -------: | ------: | --------: | --------: | -----------: | --------: | --------: | --------: | ---------: | -------: |
| `sonnet-5.5`      | `claude-sonnet-5-5` (med) |     11/20 (55%) |       3.55 |     100% |    6/20 |     13/20 |     20/20 |        28.3s |      10.6 |       90% |    $0.101 |      $2.01 |      5.5 |
| `hydrogen-low`    | `gemini-4-hydrogen` (low) |     11/20 (55%) |       3.45 |     100% |    4/20 |     14/20 |     20/20 |        40.8s |      12.1 |       84% |    $0.109 |      $2.18 |      5.0 |
| `gpt-6.2-sol`     | `gpt-6.2-sol` (med)       | **16/20 (80%)** |   **4.05** |     100% |   12/20 | **17/20** |     18/20 |        60.0s |      21.2 |       95% |    $0.124 |      $2.48 |  **6.4** |
| `hydrogen-medium` | `gemini-4-hydrogen` (med) |     15/20 (75%) |       4.00 |     100% |   12/20 |     16/20 |     19/20 |        60.5s |      17.1 |       92% |    $0.214 |      $4.27 |      3.5 |
| `neon-medium`     | `gemini-4-neon` (med)     |     14/20 (70%) |       3.50 |      90% |   13/20 |     15/20 |     15/20 |       150.8s |      13.8 |       87% |    $0.291 |      $5.83 |      2.4 |

### Pairwise Lift vs `hydrogen-medium` (480s / 50 Steps)

| Challenger     | Shared Cases |     Pass Lift | W / T / L (Score) | Mean Score Δ | Cost Δ |
| -------------- | -----------: | ------------: | :---------------: | -----------: | -----: |
| `gpt-6.2-sol`  |           20 | +1 (16 vs 15) |    5 / 11 / 4     |        +0.05 | -$1.79 |
| `neon-medium`  |           20 | -1 (14 vs 15) |    3 / 13 / 4     |        -0.50 | +$1.56 |
| `sonnet-5.5`   |           20 | -4 (11 vs 15) |    1 / 10 / 9     |        -0.45 | -$2.26 |
| `hydrogen-low` |           20 | -4 (11 vs 15) |    2 / 7 / 11     |        -0.55 | -$2.09 |

### Cross-Model Consensus (480s / 50 Steps)

| Consensus Status        | Cases | Share |
| ----------------------- | ----: | ----: |
| `all_agree`             |     5 |   25% |
| `majority_agree`        |    12 |   60% |
| `divergent`             |     2 |   10% |
| `all_blocked_or_failed` |     1 |    5% |

On 85% of cases (`all_agree` + `majority_agree`), live cross-model agreement yielded a clear
consensus without needing a fresh manual answer key.

## Results: All 8 DOM Configurations Under Tight Interactive Cap (210s / 30 Steps)

Included for full effort-level comparison (showing why `high` thinking hurts Gemini models on
multi-step browser loops).

| Candidate         | Model / Effort             | Pass (≥4/5) | Mean Score | Grounded | Completed | Med. Latency | Avg Calls | Cost/Task | Total Cost | Passes/$ |
| ----------------- | -------------------------- | ----------: | ---------: | -------: | --------: | -----------: | --------: | --------: | ---------: | -------: |
| `hydrogen-low`    | `gemini-4-hydrogen` (low)  | 11/20 (55%) |       3.35 |     100% |     19/20 |        40.8s |      10.5 |    $0.094 |      $1.87 |      5.9 |
| `sonnet-5.5`      | `claude-sonnet-5-5` (med)  | 10/20 (50%) |       3.30 |     100% |     19/20 |        28.3s |      10.2 |    $0.099 |      $1.98 |      5.1 |
| `gpt-6.2-sol`     | `gpt-6.2-sol` (med)        | 12/20 (60%) |       3.00 |      95% |     14/20 |        58.2s |      18.4 |    $0.103 |      $2.07 |      5.8 |
| `flash-3.8`       | `gemini-3.8-flash` (high)  | 10/20 (50%) |       2.40 |      80% |     11/20 |       127.7s |      19.8 |    $0.118 |      $2.35 |      4.3 |
| `hydrogen-medium` | `gemini-4-hydrogen` (med)  | 13/20 (65%) |       3.55 |      95% |     17/20 |        60.5s |      14.2 |    $0.164 |      $3.27 |      4.0 |
| `neon-medium`     | `gemini-4-neon` (med)      | 12/20 (60%) |       3.05 |      80% |     13/20 |       150.8s |      10.9 |    $0.219 |      $4.38 |      2.7 |
| `hydrogen`        | `gemini-4-hydrogen` (high) | 13/20 (65%) |       3.30 |      95% |     15/20 |       110.8s |      20.4 |    $0.250 |      $5.00 |      2.6 |
| `neon`            | `gemini-4-neon` (high)     |  8/20 (40%) |       2.10 |      70% |      9/20 |       196.5s |      19.8 |    $0.361 |      $7.21 |      1.1 |

## Results: Visual (`browser_visual_profile`) vs DOM (`browser_profile`) on 8 Cases

Visual candidates ran with native Gemini Computer Use (`enable_computer_use=True`) at 480s / 60
steps; DOM baselines are shown from the 210s / 30-step pass on the same 8 cases.

| Candidate                | Mode   | Pass (≥4/5) | Mean Score | Grounded | Completed | Med. Latency | Avg Calls | Cache Hit | Cost/Task | Passes/$ |
| ------------------------ | ------ | ----------: | ---------: | -------: | --------: | -----------: | --------: | --------: | --------: | -------: |
| `gpt-6.2-sol`            | DOM    |   4/8 (50%) |       2.75 |      88% |       5/8 |        57.1s |      18.5 |       94% |    $0.092 |      5.5 |
| `sonnet-5.5`             | DOM    |   3/8 (38%) |       3.12 |      88% |       7/8 |        26.3s |      12.8 |       92% |    $0.108 |      3.5 |
| `flash-3.8`              | DOM    |   3/8 (38%) |       2.00 |      62% |       3/8 |       130.1s |      20.1 |       91% |    $0.113 |      3.3 |
| `hydrogen-medium`        | DOM    |   4/8 (50%) |       3.12 |      88% |       6/8 |        70.0s |      15.2 |       90% |    $0.164 |      3.1 |
| `visual-flash-3.8`       | Visual |   4/8 (50%) |       2.38 |      62% |       4/8 |       329.0s |      31.5 |       76% |    $0.185 |      2.7 |
| `visual-hydrogen-medium` | Visual |   4/8 (50%) |       3.12 |      88% |       7/8 |       215.3s |      22.9 |       74% |    $0.293 |      1.7 |

Note: on the 8-case subset, `gpt-6.2-sol` and `hydrogen-medium` climbed to **6/8 (75%)** once given
the 480s / 50-step runway (matching visual's 480s budget), further widening DOM's lead over visual.
Within visual mode itself, `visual-hydrogen-medium` completed 7/8 tasks (including complex
date-picker and login-wall inspection tasks where `visual-flash-3.8` timed out at 480s).

## Category Summary (480s / 50 Steps)

| Category                      | Cases | `sonnet-5.5` | `hydrogen-low` | `gpt-6.2-sol` | `hydrogen-medium` | `neon-medium` |
| ----------------------------- | ----: | -----------: | -------------: | ------------: | ----------------: | ------------: |
| `file_download`               |     1 |    3.0 (0/1) |      2.0 (0/1) |     5.0 (1/1) |         5.0 (1/1) |     5.0 (1/1) |
| `interactive_quote_no_submit` |     2 |    4.5 (2/2) |      4.0 (2/2) |     2.5 (1/2) |         2.5 (1/2) |     2.5 (1/2) |
| `multi_page_site_navigation`  |     1 |    4.0 (1/1) |      4.0 (1/1) |     4.0 (1/1) |         4.0 (1/1) |     4.0 (1/1) |
| `multi_site_price_comparison` |     3 |    4.0 (2/3) |      3.3 (1/3) |     3.7 (2/3) |         4.7 (3/3) |     1.3 (1/3) |
| `retail_search`               |     2 |    3.5 (1/2) |      4.5 (2/2) |     4.0 (1/2) |         4.0 (2/2) |     5.0 (2/2) |
| `retail_store_specific_stock` |     1 |    4.0 (1/1) |      5.0 (1/1) |     5.0 (1/1) |         5.0 (1/1) |     5.0 (1/1) |
| `single_page_extraction`      |     5 |    3.2 (2/5) |      3.6 (3/5) |     4.8 (5/5) |         4.4 (4/5) |     4.0 (4/5) |
| `social_video`                |     1 |    4.0 (1/1) |      4.0 (1/1) |     4.0 (1/1) |         5.0 (1/1) |     5.0 (1/1) |
| `structured_table_extraction` |     1 |    5.0 (1/1) |      3.0 (0/1) |     5.0 (1/1) |         5.0 (1/1) |     5.0 (1/1) |
| `summarise_long_page`         |     1 |    3.0 (0/1) |      2.0 (0/1) |     4.0 (1/1) |         3.0 (0/1) |     5.0 (1/1) |
| `travel_hotel_availability`   |     1 |    2.0 (0/1) |      2.0 (0/1) |     2.0 (0/1) |         2.0 (0/1) |     2.0 (0/1) |
| `visual_content`              |     1 |    2.0 (0/1) |      2.0 (0/1) |     4.0 (1/1) |         2.0 (0/1) |     0.0 (0/1) |

## Caveats and Operational Notes

- Single trial per `(case, config)` on live external websites; WAF/anti-bot behavior can vary across
  runs (all candidates hit an Akamai WAF block on the hotel availability case).
- One minor Playwright walker edge case surfaced during live runs: `browser_backend.py`'s
  accessibility walker threw `TypeError: Cannot read properties of null (reading 'nodeType')` when
  invoked before `document.body` existed.
