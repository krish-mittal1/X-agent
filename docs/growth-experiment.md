# Growth experiment — 22 November 2026

Account promise: useful, relatable Hinglish observations about internet, money,
technology and culture for everyday Indians. Politics remains excluded.

## Evidence and limitations

Read-only browser sample on 22 November: IndianTechGuide 5 visible posts,
peakbengaluru 3, sagarcasm 4, NocodeTalks 5, ssh_bugs 4. This is a convenience
sample, not a complete timeline or longitudinal follower-growth study. Profile
scans can include pinned posts and reposts. Counts change and our visits may count.

- IndianTechGuide: 4,727–152,622 observed views; concrete consumer/science news.
  Example: https://x.com/IndianTechGuide/status/2124427734055018865
- Sagar: 9,690–160,987 observed views; shared cricket/sleep experiences and humour.
  Example: https://x.com/sagarcasm/status/2124439365372031179
- Peak Bengaluru: identifiable city/startup experiences, including an old pin.
  Example: https://x.com/peakbengaluru/status/2115048657976426826
- NocodeTalks: four newer posts had 164–248 views; an older tutorial pin had 7,083.
  Example: https://x.com/NocodeTalks/status/2123356173193908509
  No verified follower-growth series; do not label this account fast-growing.
- Our four visible posts: 3, 8, 19, 22 views. These are public views, not unique reach.

Observed tactics suggest hypotheses, not causal proof: specific audience relevance,
recognisable perspective, useful comparisons, and shared experiences. Established
accounts have distribution advantages that copying their cadence cannot recreate.

Current topic leads: AI economics and real utility, Indian consumer costs,
cricket moments, gaming/entertainment, year-end shopping and travel. Refresh daily;
do not hardcode predictions about December or unverified financial figures.
Research references:
https://apnews.com/article/35786b8f539ec95f16ae0500f71cfcc7
https://www.infobip.com/news/infobip-black-friday-cyber-monday-predictions-apac
https://business.x.com/en/basics/community-management

## Experiment and evaluation

Start with 2–4 original posts/day, keeping quality and factual gates. Compare
explanations, grounded humour, and useful comparisons. Replies/quotes should add
something specific to recent relevant conversations, not manufacture engagement.
Use a 12-hour author cooldown, 12-hour target age limit, and 1,000 observed views
for engagement discovery. Rank by views per hour of age: this is a rough proxy,
not measured acceleration. Video thresholds and rights settings are separate.

Collect public profile metrics every hour through the existing browser session,
serialized with other browser work. Missing counts are unknown, not observed zero.
Partial metrics are persisted in action_logs and available through
`python -m scripts.growth_report`; existing PostMetric-only dashboards do not
include partial observations. Profile visibility limits coverage; replies or older posts may not be sampled.
Do not equate public views with private impressions, profile visits or follows.

After 14 days compare median public views for snapshots nearest 24h and 48h by
format and sample size, replies/reposts, and observed engagement per view. Exclude
operator-owned likes where identifiable; public counts cannot identify those users.
Record follower changes manually until a reliable collector exists. No automatic
strategy tuning based on tiny samples, no promise of virality, no paid promotion.

Operational success: real metric rows, recent successful publishing, no repeated
originality-held draft blocking the ready queue, and auditable skipped-job reasons.
Business success must be measured separately from container health.
