#!/usr/bin/env python3
"""Extract plain text from your resumes (top level of the folder above this script).

Writes resume_texts.json next to this script. Skips cover letters, forms,
other people's files, and anything in subfolders. Contact lines are removed.
Run:  python3 extract_resumes.py
"""
import json, os, re, subprocess, sys, time, hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
FOLDER = os.path.dirname(HERE)          # the Resume folder
OUT = os.path.join(HERE, "resume_texts.json")

EXCLUDE = re.compile(
    r"(cover\s*letter|i-9|order confirmation|references|salary|"
    r"assignment|repair-the-world|questions|responses|coverletter|template|job description)",
    re.I,
)
CONTACT = re.compile(r"(\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b|@|linkedin\.com|^\s*(phone|email|address|website)\s*:)", re.I)


def label_from_name(name):
    stem = os.path.splitext(name)[0]
    stem = re.sub(os.environ.get("RESUME_NAME_PATTERN", r"$^"), " ", stem, flags=re.I)  # set RESUME_NAME_PATTERN to strip your name from labels
    stem = re.sub(r"\b(resume|cv|word doc|final|copy|v\d+|\(\d\)|20\d\d)\b", " ", stem, flags=re.I)
    stem = re.sub(r"[_^\-–—]+", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" ,.-")
    return stem or os.path.splitext(name)[0]


def extract(path):
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".docx":
            r = subprocess.run(["pandoc", path, "-t", "plain", "--wrap=none"], capture_output=True, text=True, timeout=90)
            return r.stdout
        if ext == ".pdf":
            r = subprocess.run(["pdftotext", "-layout", path, "-"], capture_output=True, text=True, timeout=90)
            return r.stdout
    except Exception as e:  # noqa
        return ""
    return ""


def clean(text):
    lines = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s or CONTACT.search(s):
            continue
        s = re.sub(r"[|+\-]{3,}", " ", s)
        s = re.sub(r"\s{2,}", " ", s)
        lines.append(s)
    return "\n".join(lines)


def main():
    files = []
    for name in sorted(os.listdir(FOLDER)):
        p = os.path.join(FOLDER, name)
        if not os.path.isfile(p):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext not in (".docx", ".pdf"):
            continue
        if EXCLUDE.search(name):
            continue
        files.append((name, p))

    # prefer .docx when a .pdf shares the same normalized stem
    by_stem = {}
    for name, p in files:
        stem = re.sub(r"[^a-z0-9]", "", os.path.splitext(name)[0].lower())
        stem = stem.replace("worddoc", "")
        cur = by_stem.get(stem)
        if cur is None or (cur[0].lower().endswith(".pdf") and name.lower().endswith(".docx")):
            by_stem[stem] = (name, p)

    out, skipped = [], []
    for name, p in sorted(by_stem.values()):
        st = os.stat(p)
        txt = clean(extract(p))
        words = len(txt.split())
        if words < 120:
            skipped.append({"file": name, "reason": f"only {words} words extracted"})
            continue
        out.append({
            "id": hashlib.sha1(name.encode()).hexdigest()[:12],
            "file": name,
            "label": label_from_name(name),
            "mtimeMs": int(st.st_mtime * 1000),
            "size": st.st_size,
            "words": words,
            "text": txt[:60000],
        })

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "folder": FOLDER,
        "count": len(out),
        "skipped": skipped,
        "resumes": out,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"extracted {len(out)} resumes, skipped {len(skipped)} -> {OUT}")
    for s in skipped:
        print("  skipped:", s["file"], "-", s["reason"])


if __name__ == "__main__":
    main()
