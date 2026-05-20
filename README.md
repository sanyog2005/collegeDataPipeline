# North India College Scraper

This folder contains a standalone Python scraper that:

- Searches DDGS for North India colleges
- Opens each college website and extracts general contact details
- Tries to find faculty and placement pages and extracts their contact details
- Writes an Excel file with separate sheets for colleges and faculty
- Saves a JSON checkpoint every 10 processed colleges so the script can resume after interruption

## Install

```bash
pip install -r requirements.txt
```

## Run

```bash
python north_india_college_scraper.py --output north_india_colleges.xlsx --checkpoint north_india_college_checkpoint.json
```

To run the full automated pipeline end to end:

```bash
python run_pipeline.py --final-output final_north_india_deep_contacts.xlsx
```

## Output

- `north_india_colleges.xlsx` contains a `Colleges` sheet and a `Faculty` sheet
- `north_india_college_checkpoint.json` stores progress for resume support
- `final_north_india_deep_contacts.xlsx` is the cleaned final workbook produced by the pipeline

## Resume behavior

If the script is stopped and started again with the same checkpoint file, it continues from the last saved college index.