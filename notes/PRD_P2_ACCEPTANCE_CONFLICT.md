# The PRD's P2 acceptance test cannot pass as written

Found while checking P2 scope against `WorkDivision.docx`. Not a code bug — the
breaker is behaving correctly. The acceptance criterion and the sourced
threshold contradict each other.

## The contradiction

`WorkDivision.docx`, Part 2, "Done when" says:

> 3 forced 503s -> OPEN; after sleep HALF-OPEN probes -> CLOSED on success.

Part 2's own config line, two paragraphs earlier, sets:

> breaker 100-window / 25% / **volume 20** / sleep 10s / 10-probes-per-5s

A breaker with `breaker_min_volume = 20` will not open on 3 failures. It needs
20 samples before it is allowed to consider the ratio at all. Measured:

```
after 1 forced 503 -> CLOSED
after 2 forced 503 -> CLOSED
after 3 forced 503 -> CLOSED      <-- the PRD's acceptance test stops here
...
first OPEN at failure #20         <-- 100.0% error rate, threshold 25%
```

## Why both numbers are right

- **Volume 20** is the sourced value. Falahah 2021 Sec. 3 uses it, and it is
  what stops a breaker tripping on two unlucky calls out of two. Dropping it
  to 3 means any dependency that fails twice is cut off, which is exactly the
  false-trip behaviour the research warns about.
- **"3 forced 503s -> OPEN"** is how circuit breakers are usually *described*
  in blog posts and videos, where the sample size is glossed over.

Neither is wrong on its own. They just cannot both be true.

## Why it matters for the demo

The demo script (Part 6) has a staged moment for "sustained 500s (OPEN ...)".
If we rehearse "hit 3 bad responses and watch it trip", it will not trip, and
we will be debugging in front of a judge. The number to rehearse is **20**,
or we change the demo to use a *lower* volume threshold for the demo policy
only — never in the library default, since the library default is the sourced
one.

## Recommendation

Keep `breaker_min_volume = 20` as the default — it is the defensible, sourced
number and it is what we will be asked to justify. Change the **acceptance
wording** in the PRD from "3 forced 503s" to "20 forced 503s", and rehearse 20.

If we want the demo to trip faster *and* stay honest, the clean way is a
separate demo policy:

```python
DEMO_POLICY = ApiPolicy(
    api_key="weather",
    base_url="https://api.open-meteo.com",
    breaker_min_volume=5,      # demo only - stated out loud on stage
)
```

and say on stage that the library default is 20 and we lowered it for the demo
so the transition is visible in the time we have. That is a stronger answer
than silently shipping a default nobody can source.

## Second acceptance line - it does pass

> "after sleep HALF-OPEN probes -> CLOSED on success"

Verified end to end: trip at 20 failures, sleep 10 s, 10 successful probes,
state returns to CLOSED. Took 10.1 s of wall time in the check, so rehearse
the sleep, not just the probe.

## Who fixes it

Not a code change, so not blocked on Aditi. It is a wording fix in
`WorkDivision.docx` and `JIZO_PRD.docx`. Pushkar to edit; needs the files
closed in Word first.