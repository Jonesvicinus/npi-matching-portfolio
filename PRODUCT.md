# Product

## Register

product

## Users

HHL (Help Hope Live) employees — non-technical administrative staff doing batch NPI match review. They work through hundreds to thousands of records per session, comparing algorithmically generated NPPES candidates against internal provider records. Their primary task per screen is a binary judgment: does this candidate match? They are doing repetitive, consequential work where errors (wrong NPI assigned to a provider) have downstream consequences.

## Product Purpose

Internal review tool for human verification of NPI matching candidates. The pipeline automatically scores ~12,000 HHL provider records against the NPPES federal registry and surfaces the best candidate per record. Reviewers approve, reject, or flag each match. Approved center decisions feed back into phase 2 matching to improve precision for professionals. No record is auto-approved — every match requires human confirmation.

## Brand Personality

Clear, precise, professional. The tool should project quiet confidence: data is laid out legibly, hierarchy is unambiguous, and the reviewer never has to hunt for what to do next. Bowst-built, Bowst-branded.

## Anti-references

- Consumer apps: no flashy transitions, no attention-grabbing motion, no delight-for-delight's-sake.
- Generic SaaS template: no Stripe-cream color palettes, no everything-rounded aesthetic, no landing-page components in a data tool.
- Healthcare clichés: no soft blue stock-photo aesthetics.
- Enterprise clutter: no SAP/Oracle density where breathing room has been traded for information quantity.

## Design Principles

1. **Review velocity first.** A reviewer may process hundreds of records per session. Every pixel of friction compounds. Decisions should be one clear action, not a hunt.
2. **Signal over decoration.** Confidence levels, match signals, and NPI numbers are the substance. Visual chrome exists to make them readable, not to compete with them.
3. **Trust through transparency.** Show exactly why a match scored what it did. Never hide low confidence behind a neutral badge. Uncertainty should read as uncertainty.
4. **Bowst-branded, not generic-tooled.** This is a Bowst-built product. The design system applies in full: Roboto Slab headings, Montserrat body, correct token values.
5. **Earn every animation.** Motion exists only to orient — collapsed rows expanding, decisions registering. Never decorative.

## Accessibility & Inclusion

WCAG 2.1 AA minimum. Decision buttons (Confirm, Flag, No Match) must be distinguishable by more than color alone. Sufficient contrast on confidence badges.
