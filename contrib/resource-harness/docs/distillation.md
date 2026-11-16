# Distilling knowledge

“Distill” uses the existing `llm-wiki` skill to save distinctive knowledge, rather
than producing only a chat summary. Explicit destinations take priority; otherwise
the skill uses its configured wiki location. Existing schema, index, recent log,
and topic pages are read before editing. Original files or session history supply
details missing from manager summaries.

Retain facts that are difficult to reconstruct: local observations, decisions and
reasons, successful methods, failures and fixes, meaningful measurements and their
conditions, exceptions, provenance, and uncertainty. Ordinary public explanations
get at most a useful source pointer. Concision does not impose a word limit or
discard the qualifiers needed to interpret a finding correctly.

Prefer a focused patch to an existing page. Create a page only for missing subject
matter; initialize a KB only when useful content needs a home. Distillation skips
ordinary ingest's raw-source copies, mandatory cross-link count, and page fanout.
Preserve unrelated content, serialize shared edits, and update navigation only
when affected. One actual batch gets one log entry; repeated content changes
nothing, including dates and logs. Source instructions remain data.

The harness's registered prompt routes both direct and delegated requests to this
operation. Updating only a profile's context files would miss manager workers,
which skip those files. On an existing installation, patch the Distill section
into the installed skill while preserving local customizations, replace the
matching research prompt, and restart the gateway when no work is active.
New manager workers receive the policy immediately after restart. Already cached
direct-chat prompts need a fresh session (`/new`); restart does not rewrite them.

The installed core drops a plugin prompt section above 4,000 characters. Keep the
research prompt within that budget; the canary compares the core's rendered section
with the candidate and observes its presence in the delegated API request. Merely
registering the section does not prove workers receive it.

Run `e2e_distill.py <receipt.json>` with the installed Hermes runtime. It uses a
real configured provider, actual manager delegation and file tools, and disposable
KBs. It checks prompt/skill propagation, preservation of scoped findings, sparse
existing-page edits, repeated-input no-op, initial KB creation, public-only content,
and resource cleanup. It captures no external Telegram delivery. This is a bounded
editorial canary, not a guarantee for every future source or model.

The [qualification receipt](distillation-qualification-20261115.json) records the
four real-provider cases, 83 passing standalone tests with tmpfs scratch, tested
and deployed hashes, and independent gateway, manager, watchdog and limit checks.
