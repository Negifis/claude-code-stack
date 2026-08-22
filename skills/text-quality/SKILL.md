---
name: text-quality
description: Before finalizing any natural-language text, including Russian, commit messages and code comments.
---

# Text Quality Layer

Apply these rules to natural-language text Claude writes: chat replies, UI strings, site/app copy, documentation, README files, code comments, commit and PR messages, emails, advertising materials, and drafts.

## Language and typography

- Default user-facing language is Russian unless the user explicitly requests another language.
- For Russian text, use clean typography: `«ёлочки»`, proper dashes where grammar requires them, concrete nouns and verbs, short paragraphs, no канцелярит.
- For code and comments, preserve the existing language of the file and project. If comments in a file are English, do not translate them to Russian without a concrete reason.

## Content quality

- Specificity is more important than smoothness. Name the actor, action, constraint, number, deadline, file, function, command, or result when known.
- Remove generic AI/business signals: empty introductions, filler conclusions, decorative formatting, repeated rhythm, excessive lists, vague intensifiers, and phrases like `важно отметить`, `в современных реалиях`, `инновационный`, `бесшовный`, `мощное решение`.
- Do not invent metrics, reviews, customer names, logos, legal claims, certifications, benchmarks, or outcomes.
- For technical documentation, code comments, and commit messages, improve clarity only: what changed, why it matters, what risk exists, and what result to expect. Do not make technical text sound salesy.

## Marketing and ads

Before writing marketing or public text, identify:

- reader;
- reader task;
- promise;
- proof;
- objections;
- next action.

For ads and promotion, use one clear angle: problem, solution, comparison, proof, curiosity, or another concrete angle that fits the platform. One promotional text should carry one main idea.

## Self-check

Before sending or saving text, check that:

- the text answers the task without introductory noise;
- known facts are concrete and unknown facts are not invented;
- there is no канцелярит, advertising hype, or generic AI prose;
- the tone fits the place: chat, UI, documentation, code comment, commit, landing page, ad, email, or PR;
- Russian typography is accurate and dashes are not used as decoration.

If these rules conflict with higher-priority instructions, factual accuracy, safety, legal precision, repository conventions, or the language already used in a code file, follow the higher-priority requirement.
