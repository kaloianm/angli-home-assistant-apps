# ExtractorFanControl

ExtractorFanControl automates bathroom/enclosed-room extractor fans based on light usage and
optional daily scheduled runs.

## Overall behavior

- Very short light usage does not start the fan.
- If light stays on long enough, fan starts and is kept alive while needed.
- When light turns off:
  - short visit -> fan stops immediately
  - longer visit -> fan keeps running for a proportional post-run duration
- Scheduled runs can also request fan operation.
- If scheduled and occupancy demands overlap, fan remains on until the later
  requirement ends.
- Manual fan toggles act as temporary authoritative override until the next
  full light cycle.

## Tests

From repository root:

```bash
PYTHONPATH=./apps python3 -m unittest discover -s ./apps/extractor_fan_control/tests -p "test_*.py"
```
