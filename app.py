import os
import subprocess
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
import uvicorn
import json

app = FastAPI()

# Global variable to prevent running multiple scrapers at once
is_running = False

def run_scraper_task():
    global is_running
    is_running = True
    try:
        # Run your existing script as a subprocess
        subprocess.run(
            ["python", "run_pipeline.py", "--final-output", "final_north_india_deep_contacts.xlsx"],
            check=True
        )
    except Exception as e:
        print(f"Scraper failed: {e}")
    finally:
        is_running = False

@app.get("/", response_class=HTMLResponse)
def index():
    return """
    <html>
        <head>
            <title>North India Scraper Control Panel</title>
            <style>
                body { font-family: Arial; padding: 40px; max-width: 600px; margin: auto; }
                button { padding: 15px 25px; font-size: 16px; margin: 10px 0; cursor: pointer; }
                .start-btn { background-color: #28a745; color: white; border: none; }
                .download-btn { background-color: #007bff; color: white; border: none; }
                .box { border: 1px solid #ddd; padding: 20px; border-radius: 8px; margin-top: 20px; }
            </style>
        </head>
        <body>
            <h2>North India Scraper Control Panel</h2>
            <div class="box">
                <button class="start-btn" onclick="fetch('/start')">🚀 Start 24-Hour Scraper</button>
                <p>Status: <span id="status">Checking...</span></p>
                <p>Rows Scraped: <span id="rows">0</span></p>
                <button class="download-btn" onclick="window.location.href='/download'">📥 Download Excel File</button>
            </div>
            
            <script>
                // Update the status every 5 seconds
                setInterval(async () => {
                    let res = await fetch('/status');
                    let data = await res.json();
                    document.getElementById('status').innerText = data.is_running ? "Running 🟢" : "Stopped 🔴";
                    document.getElementById('rows').innerText = data.rows_scraped;
                }, 5000);
            </script>
        </body>
    </html>
    """

@app.get("/start")
def start_scraper(background_tasks: BackgroundTasks):
    global is_running
    if is_running:
        return {"message": "Scraper is already running!"}
    
    # Add the scraper to background tasks so the HTTP request doesn't timeout
    background_tasks.add_task(run_scraper_task)
    return {"message": "Scraper started successfully."}

@app.get("/status")
def get_status():
    global is_running
    rows_scraped = 0
    # Read your existing checkpoint file to see progress
    if os.path.exists("scraper_checkpoint.json"):
        try:
            with open("scraper_checkpoint.json", "r") as f:
                data = json.load(f)
                rows_scraped = len(data.get("processed_urls", []))
        except:
            pass
            
    return {"is_running": is_running, "rows_scraped": rows_scraped}

@app.get("/download")
def download_excel():
    file_path = "final_north_india_deep_contacts.xlsx"
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found. The scraper hasn't finished yet or hasn't started.")
    return FileResponse(file_path, filename="north_india_contacts.xlsx")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7860)