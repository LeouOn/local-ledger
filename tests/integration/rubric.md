# LLM Evaluation Quality Rubric (task 19)

Score each LLM budget evaluation against three dimensions, 0-3 each (max 9 per ledger).

## Specificity (0-3)
- 0: generic ("spend less")
- 1: category-named ("reduce dining")
- 2: category + direction + relative magnitude ("dining is 20% over budget")
- 3: category + concrete cents figure tied to the provided aggregates

## Correctness (0-3)
- 0: contradicts the provided numbers
- 1: directionally right but quantitatively wrong
- 2: numbers match the provided aggregates
- 3: numbers match and the arithmetic logic is sound

## Actionability (0-3)
- 0: no action implied
- 1: vague action
- 2: clear single action
- 3: prioritized action(s) with expected effect

## Pass threshold
Per-ledger pass = total >= 6/9. Provider default = local if >=2 of 3 ledgers pass,
else cloud-consent.
