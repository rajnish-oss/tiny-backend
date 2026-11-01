import logging
import time
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from tinyfish_client import TinyFishClient
from job_processor import JobProcessor
from cache_manager import CacheManager
from config import COMPANY_CAREERS
from resume_parser import extract_form_data_from_bytes

STATIC_DIR = Path(__file__).parent / "static"
TINYMONITOR_DASHBOARD_URL = "https://agent.tinyfish.ai"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("tinyfish.jobs")

app = FastAPI(title="TinyFish Job Scraper")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
async def log_tinymonitor_url() -> None:
    """Show the TinyMonitor dashboard in the server terminal on startup."""
    logger.info("TinyMonitor dashboard: %s", TINYMONITOR_DASHBOARD_URL)

# Initialize components
cache = CacheManager()


class SearchRequest(BaseModel):
    role: str = ""
    location: str = ""
    seniority: str = ""
    keywords: str = ""
    portals: Optional[List[str]] = None
    company_urls: List[str] = Field(default_factory=list)
    force_refresh: bool = False


MAX_RESUME_SIZE_BYTES = 10 * 1024 * 1024


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """Serve the frontend."""
    if not STATIC_DIR.joinpath("index.html").is_file():
        raise HTTPException(status_code=404, detail="Frontend assets are not available")
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/search")
def search_jobs(data: SearchRequest):
    """
    Main search endpoint — orchestrates all 3 TinyFish APIs.

    Request body:
    {
        "role": "Software Engineer",
        "location": "Bangalore",
        "seniority": "Intern",       // Intern | Entry | Mid | Senior
        "keywords": "python react",  // optional extra keywords
        "portals": ["linkedin.com/jobs", "indeed.com"],  // optional
        "company_urls": ["https://careers.google.com/"],  // optional
        "force_refresh": false       // bypass cache
    }
    """
    role = data.role.strip()
    location = data.location.strip()
    seniority = data.seniority.strip()
    keywords = data.keywords.strip()
    portals = data.portals
    company_urls = data.company_urls
    force_refresh = data.force_refresh
    request_id = uuid.uuid4().hex[:8]
    started_at = time.perf_counter()

    logger.info(
        "[%s] Search started | role=%r location=%r seniority=%r portals=%s company_pages=%s refresh=%s",
        request_id, role, location, seniority,
        len(portals) if portals is not None else "default",
        len(company_urls), force_refresh,
    )

    if not role:
        logger.warning("[%s] Search rejected: role is required", request_id)
        return JSONResponse({"error": "Role is required"}, status_code=400)

    # Check cache first
    cache_key = CacheManager.make_cache_key(role, location, seniority, keywords)
    if not force_refresh:
        logger.info("[%s] Progress 1/5: checking cached results", request_id)
        cached_jobs, is_fresh = cache.get_cached_results(cache_key)
        if is_fresh and cached_jobs:
            logger.info(
                "[%s] Search complete from cache | jobs=%s duration=%.2fs",
                request_id, len(cached_jobs), time.perf_counter() - started_at,
            )
            return {
                "jobs": cached_jobs,
                "total": len(cached_jobs),
                "cached": True,
                "endpoints_used": ["cache"],
                "message": f"Showing {len(cached_jobs)} cached results. Click 'Refresh' for live data."
            }

    # Initialize per-request
    try:
        logger.info("[%s] Progress 2/5: initializing TinyFish client", request_id)
        client = TinyFishClient()
    except ValueError as e:
        logger.exception("[%s] TinyFish client initialization failed", request_id)
        return JSONResponse({"error": str(e)}, status_code=500)

    processor = JobProcessor()
    all_jobs = []
    endpoints_used = []
    errors = []
    user_prefs = {
        "role": role,
        "location": location,
        "seniority": seniority,
        "keywords": keywords,
    }

    # ── STEP 1: TinyFish SEARCH API ──────────────────
    # Discover job listings across portals
    try:
        logger.info("[%s] Progress 3/5: searching job portals", request_id)
        search_results = client.search_jobs(
            role=role,
            location=location,
            seniority=seniority,
            portals=portals,
            extra_keywords=keywords
        )
        search_jobs_processed = processor.process_search_results(search_results, user_prefs)
        all_jobs.extend(search_jobs_processed)
        endpoints_used.append("search")
        logger.info(
            "[%s] Portal search complete | raw_results=%s usable_jobs=%s",
            request_id, len(search_results), len(search_jobs_processed),
        )
    except Exception as e:
        errors.append(f"Search: {str(e)}")
        logger.exception("[%s] Portal search failed", request_id)

    # ── STEP 2: TinyFish FETCH API ───────────────────
    # Fetch and extract details from top search result URLs
    try:
        # Get top URLs from search results that look like actual job listings
        urls_to_fetch = []
        for job in all_jobs[:15]:  # Top 15 results
            url = job.get("apply_url", "")
            if url and url.startswith("http"):
                urls_to_fetch.append(url)

        if urls_to_fetch:
            logger.info("[%s] Progress 4/5: fetching details for %s job pages", request_id, min(len(urls_to_fetch), 10))
            fetched = client.fetch_job_pages(
                urls=urls_to_fetch[:10],  # API limit
                purpose=f"Extract detailed job listing info for {role} positions"
            )
            fetch_jobs = processor.process_fetched_content(fetched, user_prefs)
            # Enrich existing jobs or add new ones
            all_jobs = _merge_jobs(all_jobs, fetch_jobs)
            endpoints_used.append("fetch")
            logger.info("[%s] Job-page fetch complete | extracted_jobs=%s total_jobs=%s", request_id, len(fetch_jobs), len(all_jobs))
        else:
            logger.info("[%s] Progress 4/5: skipped fetch; no valid job URLs", request_id)
    except Exception as e:
        errors.append(f"Fetch: {str(e)}")
        logger.exception("[%s] Job-page fetch failed", request_id)

    # ── STEP 3: TinyFish AGENT API ───────────────────
    # Scrape dynamic company career pages
    try:
        # Agent runs are intentionally opt-in: dynamic career pages can take much
        # longer than Search and Fetch. Supply company_urls to enable this stage.
        careers_to_scrape = company_urls
        if careers_to_scrape:
            logger.info("[%s] Progress 5/5: scraping %s requested company career page(s)", request_id, len(careers_to_scrape))
        else:
            logger.info("[%s] Progress 5/5: skipped company-page Agent scrape", request_id)
        for career_url in careers_to_scrape:
            agent_result = client.agent_scrape_careers(
                url=career_url,
                role=role,
                location=location,
                seniority=seniority
            )
            if agent_result:
                agent_jobs = processor.process_agent_results(
                    agent_result, career_url, user_prefs
                )
                all_jobs.extend(agent_jobs)
        if careers_to_scrape:
            endpoints_used.append("agent")
            logger.info("[%s] Career-page scraping complete | total_jobs=%s", request_id, len(all_jobs))
    except Exception as e:
        errors.append(f"Agent: {str(e)}")
        logger.exception("[%s] Career-page scraping failed", request_id)

    # ── FINAL: Sort by relevance & deduplicate ───────
    all_jobs = _deduplicate(all_jobs)
    all_jobs.sort(key=lambda j: j.get("relevance_score", 0), reverse=True)

    # Cache the results
    cache.save_results(cache_key, all_jobs, user_prefs)
    logger.info(
        "[%s] Search complete | jobs=%s endpoints=%s errors=%s duration=%.2fs",
        request_id, len(all_jobs), ",".join(endpoints_used) or "none", len(errors),
        time.perf_counter() - started_at,
    )

    return {
        "jobs": all_jobs,
        "total": len(all_jobs),
        "cached": False,
        "endpoints_used": endpoints_used,
        "errors": errors if errors else None,
        "message": f"Found {len(all_jobs)} listings using {', '.join(endpoints_used)} endpoints."
    }


@app.post("/api/resume/parse")
async def parse_resume(resume: UploadFile = File(...)):
    """Parse a PDF resume into search-form preferences without persisting the file."""
    request_id = uuid.uuid4().hex[:8]
    filename = resume.filename or "resume.pdf"

    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Please upload a PDF resume.")

    resume_bytes = await resume.read()
    if not resume_bytes:
        raise HTTPException(status_code=400, detail="The uploaded resume is empty.")
    if len(resume_bytes) > MAX_RESUME_SIZE_BYTES:
        raise HTTPException(status_code=413, detail="Resume files must be 10 MB or smaller.")

    try:
        logger.info("[%s] Parsing resume | size=%s bytes", request_id, len(resume_bytes))
        form_data = extract_form_data_from_bytes(resume_bytes)
        logger.info("[%s] Resume parsing complete | role=%r skills=%s", request_id, form_data.role, len(form_data.keywords))
        return form_data.model_dump()
    except KeyError:
        logger.exception("[%s] Resume parsing is not configured", request_id)
        raise HTTPException(status_code=503, detail="Resume parsing requires GROQ_API_KEY in backend/.env.")
    except Exception:
        logger.exception("[%s] Resume parsing failed", request_id)
        raise HTTPException(status_code=422, detail="We could not read that resume. Please try another PDF.")


@app.post("/api/cache/clear")
def clear_cache():
    """Clear the job results cache."""
    cache.clear_cache()
    logger.info("Search cache cleared")
    return {"message": "Cache cleared successfully."}


@app.get("/api/health")
def health():
    """Health check endpoint."""
    return {
        "status": "ok",
        "service": "TinyFish Job Scraper",
        "endpoints": ["search", "fetch", "agent"],
    }


def _deduplicate(jobs):
    """Remove duplicate jobs based on ID."""
    seen = set()
    unique = []
    for job in jobs:
        jid = job.get("id", "")
        if jid and jid not in seen:
            seen.add(jid)
            unique.append(job)
    return unique


def _merge_jobs(existing, new_jobs):
    """Merge new job data into existing list, enriching where possible."""
    existing_by_url = {j.get("apply_url", ""): j for j in existing}
    for nj in new_jobs:
        url = nj.get("apply_url", "")
        if url in existing_by_url:
            # Enrich existing entry with fetched data
            ej = existing_by_url[url]
            if not ej.get("company") and nj.get("company"):
                ej["company"] = nj["company"]
            if not ej.get("location") and nj.get("location"):
                ej["location"] = nj["location"]
            if len(nj.get("snippet", "")) > len(ej.get("snippet", "")):
                ej["snippet"] = nj["snippet"]
            ej["relevance_score"] = max(
                ej.get("relevance_score", 0),
                nj.get("relevance_score", 0)
            )
        else:
            existing.append(nj)
    return existing


if __name__ == "__main__":
    import uvicorn

    print("\n[TinyFish] Job Scraper starting...")
    print("   Open http://localhost:5000 in your browser\n")
    uvicorn.run("app:app", host="0.0.0.0", port=5000, reload=True)
