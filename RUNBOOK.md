# Runbook: running Job Radar as a scheduled agent

This is the procedure a scheduled AI agent follows to operate the scanner unattended, five times a day. It assumes the agent has a shell, can read and write files, can call a small document store (I use the database behind a published tracker page; a folder of JSON files is equivalent), and can optionally reach a laptop where the resume folder lives. Nothing in it needs judgment; the design goal was that an agent could run it without asking anyone anything.

## Database layout

- `days/<YYYY-MM-DD>` one document per scan: `{date, generated_at, count, new_count, repeat_count, contract_count, fresh_count, jobs: [...], stats: {...}}`. Rerunning the same date overwrites the day's document; the scanner ignores the current date when deciding what was "previously listed", so same-day reruns do not double count.
- `status/<YYYY-MM>` a map of job id to `{status: applied|saved|skipped, at, title, company, url}`, written by the tracker page when a button is pressed. Any non-empty status keeps a job out of future lists.
- `resumes/<id>` one document per resume: `{id, file, label, mtimeMs, words, text}`. The fallback when the laptop is unreachable.
- `meta/summary` `{date, last_run, count, new_count, contract_count, fresh_count, resumes_on_file, resume_source, total_fetched, sources_ok, sources_failed}`.

## Steps

1. **Set up.** Make `work/db` and `work/out`. Get today's date in the home time zone and call it DATE. Check `python3 -c "import requests, sklearn"` and install the two packages if that fails.
2. **Dump the database** into `work/db`: the last 60 `days` documents, every `status` document, and up to 300 `resumes` documents. If `work/db/days/DATE.json` already exists, keep a copy as `work/prev_today.json` so step 8 can tell what is new since the last run today.
3. **Sync resumes** from the laptop, skipping the whole step on any failure: run `extract_resumes.py` in the Job Scanner subfolder of the resume folder, then copy the resulting `resume_texts.json` into `work/`. On failure the scan uses the resume copies from step 2.
4. **Scan.** `python3 scanner.py scan --resumes work/resume_texts.json --db-dir work/db --out work/out --date DATE --limit 20`, without `--resumes` if step 3 failed. Read `work/out/summary.md`. Roles posted within the last day carry `fresh: true` and are listed first.
5. **Write results back**: `work/out/day.json` to `days/DATE`, `work/out/meta.json` to `meta/summary`, and any files in `work/out/resume_writes/` to `resumes/<id>` (usually none). If a document already exists, read it first and resend with its version token.
6. **Top up** only if `day.json` has fewer than 12 jobs: run up to six web searches shaped like the target families, write any genuine, currently open, remote-US postings to `work/extra.json` as `{title, company, url, location, desc, etype_raw, posted, tags}`, rerun step 4 with `--extra work/extra.json`, and redo step 5.
7. **Notify** only when at least one of these is true: a role is on the list that was not there at the last run today, this is the first run of the day, or a source failed. Lead with the number of roles posted in the last 24 hours and the top three by score, then the total, the contract or part-time count, whether the resume sync worked, and any failed sources from `stats.failed_sources`.
8. **Never** email anyone, apply to anything, or contact an employer. The scanner reads; a person decides.

## Scoring, for tuning

Weights and knobs are in the `scoring` block of `targets.json`; word lists and cutoffs default from `SCORING_DEFAULTS` in `scanner.py` and can be overridden there.

- **Skills overlap, 40.** 45 percent TF-IDF similarity to the best-matching resume (scaled between 0.035 and 0.13), 30 percent the skills lexicon (weighted hits, full marks at 14), 25 percent the share of the posting's requirements wording (the text after a "Qualifications" style heading) that the matched resume also uses. A hard requirement listed in `skill_gaps` costs a tenth of the skills score and is written to `gaps` on the job; "preferred" and "a plus" wording is ignored.
- **Seniority fit, 25.** Title level earns `level_points` (5 executive, 4 director, 3 manager or senior consultant, 2 individual contributor, 1 entry). Years asked for are compared with `candidate_years`; more than three above costs 10, two or fewer on a manager-or-below title costs 5. Hourly pay under the floor caps the part at 8. Roles under `min_seniority` are not listed at all and are counted in `stats.seniority_mismatch`.
- **Domain fit, 20.** Full marks for the candidate's home domains, stepping down through adjacent fields, the health sector generally, neighboring causes, general nonprofit work, and unrelated sectors. The word lists are in `SCORING_DEFAULTS`.
- **Practical fit, 15.** Location 6 (any US state or the home states; 3 when tied to another state; 0 when the posting requires living elsewhere), work arrangement 4 (contract, part-time, fractional, interim; 2 full-time; 1 not stated), pay 5 (5 at the hourly or salary target, 4 other posted pay, 3 not posted, 1 low), minus 2 for travel of half the time or more.

Each job in `day.json` carries `parts`, `parts_max`, and `part_notes` so a tracker page can show why a role scored what it did. Minimum listed score is 50. Fresh roles get reserved slots at 55 or more and lead the list at 60 or more. Watched employers are listed whatever they score. `--raw-cache raw.pkl` saves one fetch of every posting and reuses it on later runs so scoring changes can be compared without refetching.

## Things learned the hard way

- Read remote status from the body, not the location tag. Boards label roles "Remote" that require daily attendance, and bury "fully remote within the US" in a benefits list.
- Read employment type from the posting's own words. A 1099 role is routinely filed under "FullTime".
- Strip shift and schedule suffixes before deduplicating, or one nursing role posted for five schedules becomes five entries.
- Every source breaks eventually. Report failed sources every run so a quiet week is distinguishable from a broken scraper.
- Supply in a narrow niche is thin, so the list repeats from day to day. The status map is what keeps it readable.
