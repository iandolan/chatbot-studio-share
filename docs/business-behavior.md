# Business behavior profiles

Each bot has an optional profile under Setup > Configure business behavior, or the Business behavior tab. Choose a template, load defaults, edit the settings, preview the configuration, and save. Templates cover general business, ecommerce, personal injury intake, local services, and document Q&A.

The profile controls the purpose, response length, contact capture preference, allowed actions, intake questions, boundaries, and human help expectations. Loading a template updates the editor only. Saving applies it to subsequent messages. Existing bots retain legacy behavior until enabled. Turning a profile off restores the existing settings.

## Enforcement

The server filters model actions in both streaming and regular replies before storing or executing them. Enabled profiles block disabled actions, contact forms without a lead rule, repeated contact forms after a recorded lead, and repeated handoff actions while already waiting for a person. Redirect URLs still require the existing allowlist. The public lead endpoint rejects submissions when profile settings disable capture.

Conversational consent, intake question order, response length, and boundaries are prompt guidance, not deterministic rules. The current implementation has no persistent intake state machine or Jev intent routing. Text can still make inaccurate claims even when its action is blocked, especially when streamed. Test with the selected live model before publishing.

The configuration preview makes no model calls, saves no settings, and creates no conversations. It lists available actions, missing prerequisites, and suggested test questions. It does not simulate or grade answers. Use the existing test chat for that.

Orders, refunds, calendars, and confirmed bookings are not connected capabilities. A contact request remains a contact request. Existing tone, guardrails, lead forms, redirects, and notifications remain in their own tabs.

## Jev screening correction

Screening now accepts SQLite rows and uses a 0.5 second network timeout instead of 20 seconds. This is a socket timeout, not an end-to-end deadline or a measured latency guarantee. Jev is not newly enabled by this change. Live model performance and classifier accuracy have not been benchmarked.

## Storage and validation

An additive, repeatable migration adds bots.behavior_config with an empty JSON default. The admin API validates profile types, choices, list sizes, text limits, and supported actions. The profile is omitted from public widget configuration. No production database is copied or replaced.

## Verification

151 offline tests pass, including both response paths, disabled direct lead submissions, repeated migrations, invalid settings, and the database-row screening regression. Browser checks verified template loading, preview warnings, save, and persistence after navigation. No live model evaluation or production deployment was performed.
