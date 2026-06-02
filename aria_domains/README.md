# aria_domains/

Domain priors for Aria — small markdown briefs that load into her system prompt
when a turn touches a relevant domain.

## What these are NOT

NOT general knowledge. Aria (Opus 4.7) already knows business, law, finance,
fatherhood, risk, partnerships as topics — she has read the books. The briefs
do not teach her facts.

## What these ARE

The frame **you** want her to take when discussing each domain with **you**.
Your frameworks. Your red flags. The questions you want her to ask. The
specific stance — pushback over politeness, specifics over frameworks,
direct over careful — that turns "smart assistant on a topic" into
"partner who knows how you think about this."

Read each file as if you were instructing a colleague joining a sensitive
meeting about how you want them to engage. That's the level of detail.

## How they're picked

`aria_domains.py` runs a keyword classifier on each user message. Matched
domains' briefs are concatenated (cap 3) and injected into Aria's system
prompt for that turn. Briefs are NOT loaded for trivial turns — "what time
is it" doesn't pull in business.md.

## Adding a new domain

1. Create `aria_domains/<name>.md` with the same shape as the existing files.
2. Add a keyword regex for the domain in `aria_domains.py:_DOMAIN_KEYWORDS`.
3. That's it. Loaded on next restart.

## Editing existing domains

You're encouraged to. The starter content is a scaffold, not the answer.
Over time these files should drift toward what YOU actually want her to do
with each domain — not what a generic profile-of-a-thoughtful-friend would.

The more specific these get, the more she becomes yours.

## Current domains

- `business.md` — running his own thing, unit economics, leverage, friction
- `legal.md` — contract reading, red flags, when to call the lawyer
- `fatherhood.md` — his kids, presence over advice, when to name the heavy
- _(more to follow: risk, finance, partnerships)_
