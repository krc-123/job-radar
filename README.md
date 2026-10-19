# Job Radar

A remote-job scanner that pulls postings from fourteen sources, filters them hard, scores each one out of 100 against a set of target role families and against the text of your own resumes, and writes a short daily list. I built it for my own search for part-time nonprofit leadership and remote clinical leadership work, and it has run five times a day since September 2026 as a scheduled agent that reads the previous days' results, reruns the scan, and pushes the day's new roles to my phone.

It is one Python file plus three JSON files. No framework, no database server.

## What it does

1. **Fetch.** Pulls every open posting from the company boards in `companies.json` (Greenhouse, Lever, Ashby, and Workday tenants) and from the public feeds of Remotive, RemoteOK, Jobicy, Himalayas, Working Nomads, We Work Remotely, Tech Jobs for Good, Fractional Jobs, Philanthropy News Digest, and Idealist. Sources run in parallel; a failed source is reported, never fatal.
2. **Filter.** Remote and US-eligible only, by reading the posting body rather than trusting the location tag. Title exclusions for roles that need a license the candidate does not hold. Employment type (contract, part-time, full-time) read from the posting's own words, so a 1099 role filed under "FullTime" is still caught. An optional `focus` block narrows the list to, for example, fractional and part-time roles at nonprofits, with an `also_list` lens that lets a second category through.
3. **Match.** Each posting is assigned to one of 34 role families (regex on the title, with a "needs health context" flag for ambiguous titles like Product Manager). Nonprofit status is inferred from the posting text with strict and loose patterns depending on the board.
4. **Score.** Four parts, weights configurable: skills overlap 40, seniority fit 25, domain fit 20, practical fit 15. The seniority check reads "N+ years" requirements out of the posting and compares them with the candidate's years; a posting that misses the seniority floor is dropped with a one-line reason. Requirements sections are detected by heading and checked term by term against the resume vocabulary, so the output can say which requirements the resumes already cover and which they do not.
5. **Resume fit.** TF-IDF cosine similarity between every posting and every resume on file, in two passes (the full text, and a headline pass of title plus opening paragraph against resume label plus summary), averaged. The list names the resume that fits each posting best. `extract_resumes.py` turns a folder of .docx and .pdf resumes into the JSON the scanner reads, stripping contact lines.
6. **Remember.** Previous days' lists and any status you set (applied, saved, skipped) keep a role from being listed twice. Roles posted in the last 24 hours get reserved slots and are listed first. Watched employers bypass the focus filter and the age cutoff.
7. **Report.** `day.json` for a tracker page or database, `meta.json` for a summary tile, `summary.md` to read, and `candidates.json` for debugging why something did or did not make the list, including a count of rejections by reason.

## Run it

```
pip install -r requirements.txt
python3 extract_resumes.py                      # from a "Job Scanner" subfolder of your resume folder
python3 scanner.py scan --resumes resume_texts.json --db-dir db --out out --date 2026-10-18 --limit 20
```

`--db-dir` is a folder of JSON documents from earlier runs: `days/<date>.json`, `status/<month>.json`, and `resumes/<id>.json`. Any key-value store that can dump to files works; I use a published tracker page's database, but a folder on disk is fine. `--extra` accepts a JSON list of postings found by hand so they can be scored alongside the rest. `--raw-cache` pickles the fetched postings so you can tune the scoring without hitting the sources again.

Idealist needs its public search credentials in `IDEALIST_APP` and `IDEALIST_KEY` (they are visible in the site's own page requests); without them that source is skipped. Set `JOB_RADAR_UA` to a user agent that names you, which is good manners with a scraper.

## Configure it

- `targets.json`: role families and weights, the health-context regex, title and description exclusions, the skills lexicon with points, employment type patterns, US and non-US location patterns, the `focus` block, the scoring weights, and the fundraising exclusion. Every block carries a `note` explaining what it does. This file is the shape of one person's search; edit it to the shape of yours.
- `companies.json`: the boards to scan, with optional tags (`ai`, `nonprofit`, `maternal`) that earn a scoring bonus, and `watch: true` for employers whose roles should always be listed. Add a board by appending `{ats, slug, name}`.

## Running it unattended

`RUNBOOK.md` is the procedure a scheduled agent follows every four hours: pull the previous results, sync resumes from the laptop if it is reachable, run the scan, write the results back, top up with web searches when the list is thin, and notify only when something changed. The scanner was written so that an agent with a shell and a few file tools could operate it without judgment calls.

## Honest limits

Boards change their markup; a source that worked last month can go quiet, which is why the stats block reports failed sources every run. The scoring is a heuristic tuned by reading its mistakes for several weeks, not a model, and the resume fit rewards vocabulary overlap, so a resume written in a posting's language will always look like a better fit than a stronger resume written differently. Treat the score as a sort order, not a verdict.

## License

MIT.
