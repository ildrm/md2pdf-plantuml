import re
import subprocess
from pathlib import Path
from multiprocessing import Pool, Lock, cpu_count
import traceback
import os
import shutil

# -------- CONFIG --------
md_file = Path(r"F:\MD-Proj\book.md")                 # Input Markdown
plantuml_jar = Path(r"F:\MD-Proj\libs\plantuml.jar")  # Path to PlantUML .jar
output_dir = Path(r"F:\MD-Proj\diagrams")             # Output dir for generated diagrams
output_dir.mkdir(exist_ok=True)
temp_md = Path(r"F:\MD-Proj\book_tmp.md")             # Temp Markdown passed to Pandoc
pdf_file = md_file.with_suffix(".pdf")                # Final PDF output
pandoc_exe = "pandoc"                                 # Pandoc executable
log_file = Path(r"F:\MD-Proj\conversion.log")         # Log file

# -------- GLOBAL LOG LOCK --------
log_lock = Lock()

def log_print(message: str):
    """Thread-safe log writer."""
    with log_lock:
        print(message)
        with open(log_file, "a", encoding="utf-8") as log:
            log.write(message + "\n")

# -------- REGEXES FOR UML BLOCKS --------
RE_FENCED = re.compile(r"(```\s*plantuml[^\n]*\n)(.*?)(\n```)", re.IGNORECASE | re.DOTALL)
RE_STARTEND = re.compile(r"(@startuml\b.*?@enduml)", re.IGNORECASE | re.DOTALL)

def find_uml_blocks(md_text: str):
    """
    Find both fenced ```plantuml blocks and @startuml ... @enduml blocks.
    Returns a list of dicts with:
      - 'span': tuple(start, end) of the match in the original text
      - 'original': exact original text to replace later
      - 'code': PlantUML source for rendering
      - 'kind': 'fenced' or 'startend'
    """
    matches = []

    # Fenced blocks
    for m in RE_FENCED.finditer(md_text):
        original = m.group(0)
        code = m.group(2)
        matches.append({
            "span": (m.start(), m.end()),
            "original": original,
            "code": code,
            "kind": "fenced",
        })

    # @startuml ... @enduml blocks
    for m in RE_STARTEND.finditer(md_text):
        original = m.group(1)
        code = original  # keep as-is
        matches.append({
            "span": (m.start(1), m.end(1)),
            "original": original,
            "code": code,
            "kind": "startend",
        })

    # Sort by position to ensure stable replacement order
    matches.sort(key=lambda d: d["span"][0])
    return matches

def ensure_wrapped(code: str) -> str:
    """Ensure the code has @startuml/@enduml around it."""
    low = code.lower()
    if "@startuml" in low and "@enduml" in low:
        return code
    return f"@startuml\n{code}\n@enduml"

def escape_unescaped_dollars(s: str) -> str:
    """
    Escape all unescaped $ so LaTeX won't interpret them as math mode.
    IMPORTANT: Call this on the final Markdown passed to Pandoc, NOT on PlantUML sources.
    """
    return re.sub(r"(?<!\\)\$", r"\\$", s)

# -------- PAGE BREAK HANDLING --------
RE_PAGEBREAK_LINE = re.compile(r"^[ \t]*-{3}[ \t]*$", re.MULTILINE)

def split_yaml_header(md_text: str):
    """
    Split YAML header (if present) from the rest of the document.
    Returns (yaml_header, body). If no header, yaml_header is '' and body is original.
    """
    text = md_text.lstrip('\ufeff')  # drop BOM if any
    if text.startswith("---\n") or text.startswith("---\r\n"):
        lines = text.splitlines(keepends=True)
        header = []
        body_start_idx = None
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() in ("---", "..."):
                body_start_idx = i + 1
                break
            header.append(line)
        if body_start_idx is not None:
            yaml_header = "---\n" + "".join(header) + lines[body_start_idx - 1]
            body = "".join(lines[body_start_idx:])
            return yaml_header, body
    # No YAML header
    return "", md_text

def apply_page_breaks(md_text: str) -> str:
    """
    Convert lines that are exactly '---' to LaTeX page breaks (\newpage),
    excluding the YAML header area.
    """
    yaml_header, body = split_yaml_header(md_text)
    body = RE_PAGEBREAK_LINE.sub(r"\n\\newpage\n", body)
    return yaml_header + body

# -------- PANDOC WITH FONT FALLBACK --------
def run_pandoc_with_font_fallback(md_path: Path, pdf_path: Path, pandoc_exe: str = "pandoc") -> int:
    """
    Try Pandoc with several (mainfont, monofont) pairs common on Windows.
    Returns the subprocess return code; 0 means success.
    """
    font_pairs = [
        ("Times New Roman", "Consolas"),
        ("Cambria", "Consolas"),
        ("Calibri", "Consolas"),
        ("Arial", "Consolas"),
        ("Times New Roman", "Lucida Console"),
        ("Times New Roman", "Courier New"),
        # If installed, these are better for Unicode box-drawing:
        ("Times New Roman", "Noto Sans Mono"),
        ("Times New Roman", "DejaVu Sans Mono"),
    ]

    if not shutil.which(pandoc_exe):
        log_print(f"❌ '{pandoc_exe}' not found in PATH.")
        return 127

    for mainfont, monofont in font_pairs:
        log_print(f"🔤 Trying fonts -> mainfont='{mainfont}', monofont='{monofont}'")
        cmd = [
            pandoc_exe,
            str(md_path),
            "-o", str(pdf_path),
            "--pdf-engine=xelatex",
            "-V", f"mainfont={mainfont}",
            "-V", f"monofont={monofont}",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.stdout.strip():
            log_print(result.stdout)
        if result.stderr.strip():
            log_print(result.stderr)
        if result.returncode == 0 and pdf_path.exists():
            log_print(f"✅ PDF successfully generated with fonts: {mainfont} / {monofont}")
            return 0
        else:
            log_print(f"⚠️ Pandoc attempt failed with fonts: {mainfont} / {monofont}")

    log_print("🪂 Falling back to Pandoc defaults (no explicit fonts).")
    result = subprocess.run(
        [pandoc_exe, str(md_path), "-o", str(pdf_path), "--pdf-engine=xelatex"],
        capture_output=True,
        text=True
    )
    if result.stdout.strip():
        log_print(result.stdout)
    if result.stderr.strip():
        log_print(result.stderr)
    return result.returncode

# -------- PLANTUML FUNCTION (PARALLEL) --------
def process_uml(args):
    """
    Render a single PlantUML block to PNG.
    Returns tuple(i, success, path_or_error).
    """
    i, code_text, plantuml_jar, output_dir = args
    puml_file = output_dir / f"diagram{i}.puml"
    png_file = output_dir / f"diagram{i}.png"

    try:
        if png_file.exists():
            log_print(f"⏭ Diagram {i} already exists, skipping: {png_file}")
            return (i, True, png_file)

        # Do NOT touch dollar signs; LaTeX will never see this text.
        uml_source = ensure_wrapped(code_text)

        puml_file.write_text(uml_source, encoding="utf-8")

        result = subprocess.run(
            ["java", "-jar", str(plantuml_jar), "-tpng", str(puml_file)],
            capture_output=True,
            text=True
        )

        log_print(f"\n--- PlantUML Diagram {i} ---")
        if result.stdout.strip():
            log_print(result.stdout)
        if result.stderr.strip():
            log_print(result.stderr)

        if result.returncode != 0:
            log_print(f"❌ Error generating diagram {i} (exit code {result.returncode})")
            return (i, False, result.stderr.strip())
        else:
            log_print(f"✅ Diagram {i} generated: {png_file}")
            return (i, True, png_file)

    except Exception as e:
        tb = traceback.format_exc()
        log_print(f"❌ Exception generating diagram {i}: {e}\n{tb}")
        return (i, False, str(e))

# -------- MAIN WORKFLOW --------
if __name__ == "__main__":
    # Optional: Unicode output for Windows console
    try:
        os.system("chcp 65001 >nul")
    except Exception:
        pass

    log_file.unlink(missing_ok=True)
    log_print("🚀 Starting diagram extraction and conversion...")

    # -------- READ MARKDOWN --------
    md_text = md_file.read_text(encoding="utf-8")

    # -------- EXTRACT UML BLOCKS (BOTH FORMS) --------
    blocks = find_uml_blocks(md_text)
    total = len(blocks)
    log_print(f"📊 Found {total} UML diagrams.")

    if total == 0:
        log_print("⚠️ No diagrams found. Skipping PlantUML generation.")
    else:
        # -------- PARALLEL PROCESSING --------
        num_proc = min(cpu_count(), 6)
        log_print(f"🧩 Using {num_proc} parallel processes...")

        args = [(i, blk["code"], plantuml_jar, output_dir) for i, blk in enumerate(blocks, start=1)]

        with Pool(processes=num_proc) as pool:
            results = pool.map(process_uml, args)

        # -------- UPDATE MARKDOWN: replace each UML block with an image --------
        for (i, success, info), blk in zip(results, blocks):
            if success:
                safe_url = Path(info).as_posix()
                # Limit image width to text width to avoid "Float too large" warnings
                replacement = f"![Diagram {i}]({safe_url}){{ width=100% }}"
            else:
                # Visible error note in the document
                replacement = f"**Diagram {i} failed to generate: {info}**"

            # Replace only the first occurrence of the exact original block
            md_text = md_text.replace(blk["original"], replacement, 1)

    # -------- PAGE BREAKS: convert --- lines to \newpage (excluding YAML header) --------
    md_text = apply_page_breaks(md_text)

    # -------- SAVE TEMP MARKDOWN --------
    # Escape any remaining unescaped $ in the Markdown that Pandoc will see.
    md_text_safe = escape_unescaped_dollars(md_text)
    temp_md.write_text(md_text_safe, encoding="utf-8")

    # -------- CONVERT TO PDF USING PANDOC (with font fallback) --------
    log_print("\n--- Starting Pandoc conversion ---")
    rc = run_pandoc_with_font_fallback(temp_md, pdf_file, pandoc_exe=pandoc_exe)

    if rc != 0:
        log_print("❌ Pandoc failed to generate PDF")
    elif pdf_file.exists():
        log_print(f"✅ PDF successfully generated: {pdf_file}")
    else:
        log_print("❌ PDF was not created. Check Pandoc/LaTeX setup.")

    log_print("🏁 Conversion process completed.")
