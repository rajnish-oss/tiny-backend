"""
Job Processor — Parses raw TinyFish output into structured,
deduplicated job listings with relevance scoring.
"""

import re
import json
import hashlib
from datetime import datetime


class JobProcessor:
    """Processes raw scrape data into clean, ranked job listings."""

    def __init__(self):
        self.seen_ids = set()

    def process_search_results(self, search_results, user_prefs):
        """
        Convert raw TinyFish Search API results into structured job listings.

        Args:
            search_results: Raw results from TinyFish Search
            user_prefs: Dict with role, location, seniority, keywords

        Returns:
            List of processed job dicts
        """
        jobs = []
        for result in search_results:
            title = result.get("title", "")
            snippet = result.get("snippet", result.get("description", ""))
            url = result.get("url", result.get("link", ""))
            source = result.get("source_portal", "web")

            if not url or not title:
                continue

            # Skip non-job URLs
            if self._is_noise_url(url, title):
                continue

            job = {
                "id": self._generate_id(title, url),
                "title": self._clean_title(title),
                "company": self._extract_company(title, snippet, source),
                "location": self._extract_location(snippet, user_prefs.get("location", "")),
                "type": self._extract_job_type(title, snippet),
                "seniority": self._extract_seniority(title, snippet),
                "snippet": snippet[:300] if snippet else "",
                "apply_url": url,
                "source": self._portal_display_name(source),
                "posted_date": self._extract_date(snippet),
                "relevance_score": 0,
                "scraped_at": datetime.now().isoformat(),
            }

            # Deduplicate
            if job["id"] not in self.seen_ids:
                self.seen_ids.add(job["id"])
                job["relevance_score"] = self._score_relevance(job, user_prefs)
                jobs.append(job)

        return jobs

    def process_fetched_content(self, fetched_pages, user_prefs):
        """
        Extract structured job data from TinyFish Fetch API markdown content.

        Args:
            fetched_pages: Raw results from TinyFish Fetch
            user_prefs: User preference dict

        Returns:
            List of enriched job dicts
        """
        enriched_jobs = []
        for page in fetched_pages:
            text = page.get("text", page.get("content", page.get("markdown", "")))
            url = page.get("url", "")
            title = page.get("title", "")
            links = page.get("links", [])

            if not text:
                continue

            # Try to extract individual job listings from the page content
            extracted = self._extract_jobs_from_markdown(text, url, links)

            if extracted:
                for job in extracted:
                    job["relevance_score"] = self._score_relevance(job, user_prefs)
                    if job["id"] not in self.seen_ids:
                        self.seen_ids.add(job["id"])
                        enriched_jobs.append(job)
            else:
                # Treat the whole page as a single listing
                job = {
                    "id": self._generate_id(title, url),
                    "title": self._clean_title(title),
                    "company": self._extract_company(title, text, ""),
                    "location": self._extract_location(text, user_prefs.get("location", "")),
                    "type": self._extract_job_type(title, text),
                    "seniority": self._extract_seniority(title, text),
                    "snippet": text[:400],
                    "apply_url": url,
                    "source": "Direct Page",
                    "posted_date": self._extract_date(text),
                    "relevance_score": 0,
                    "scraped_at": datetime.now().isoformat(),
                }
                job["relevance_score"] = self._score_relevance(job, user_prefs)
                if job["id"] not in self.seen_ids:
                    self.seen_ids.add(job["id"])
                    enriched_jobs.append(job)

        return enriched_jobs

    def process_agent_results(self, agent_data, source_url, user_prefs):
        """
        Process TinyFish Agent API output (already structured JSON).

        Args:
            agent_data: Raw agent result (usually JSON string or dict)
            source_url: The URL the agent scraped
            user_prefs: User preference dict

        Returns:
            List of processed job dicts
        """
        jobs = []

        # Agent often returns JSON string — parse it
        if isinstance(agent_data, str):
            try:
                # Try to extract JSON from the response
                json_match = re.search(r'\[.*\]', agent_data, re.DOTALL)
                if json_match:
                    agent_data = json.loads(json_match.group())
                else:
                    agent_data = json.loads(agent_data)
            except json.JSONDecodeError:
                print(f"[PROCESSOR] Could not parse agent response as JSON")
                return jobs

        if isinstance(agent_data, dict):
            agent_data = [agent_data]

        if not isinstance(agent_data, list):
            return jobs

        for item in agent_data:
            if not isinstance(item, dict):
                continue

            title = item.get("title", "")
            apply_url = item.get("apply_url", item.get("url", item.get("link", source_url)))
            company = item.get("company", "")

            job = {
                "id": self._generate_id(title, apply_url),
                "title": title,
                "company": company,
                "location": item.get("location", ""),
                "type": item.get("type", self._extract_job_type(title, "")),
                "seniority": self._extract_seniority(title, item.get("description", "")),
                "snippet": (item.get("description", ""))[:400],
                "apply_url": apply_url,
                "source": f"Agent ({self._extract_domain(source_url)})",
                "posted_date": item.get("posted_date", ""),
                "relevance_score": 0,
                "scraped_at": datetime.now().isoformat(),
            }
            job["relevance_score"] = self._score_relevance(job, user_prefs)
            if job["id"] not in self.seen_ids:
                self.seen_ids.add(job["id"])
                jobs.append(job)

        return jobs

    # ──────────────────────────────────────────────────
    # RELEVANCE SCORING
    # ──────────────────────────────────────────────────
    def _score_relevance(self, job, prefs):
        """Score 0-100 based on how well a job matches user preferences."""
        score = 0
        text_blob = f"{job.get('title', '')} {job.get('snippet', '')} {job.get('company', '')}".lower()

        # Role match (0-40 pts)
        role = prefs.get("role", "").lower()
        if role:
            role_words = role.split()
            matched_words = sum(1 for w in role_words if w in text_blob)
            score += int((matched_words / max(len(role_words), 1)) * 40)

        # Location match (0-25 pts)
        location = prefs.get("location", "").lower()
        if location:
            if location in job.get("location", "").lower():
                score += 25
            elif location in text_blob:
                score += 15
            elif "remote" in text_blob:
                score += 10

        # Seniority match (0-20 pts)
        seniority = prefs.get("seniority", "").lower()
        if seniority:
            if seniority in text_blob:
                score += 20
            # Map common synonyms
            seniority_map = {
                "intern": ["intern", "internship", "trainee", "apprentice"],
                "entry": ["entry", "junior", "fresher", "graduate", "new grad"],
                "mid": ["mid", "intermediate", "2-5 years", "3+ years"],
                "senior": ["senior", "lead", "staff", "principal", "5+ years"],
            }
            synonyms = seniority_map.get(seniority, [])
            if any(s in text_blob for s in synonyms):
                score += 15

        # Keyword boost (0-15 pts)
        keywords = prefs.get("keywords", "").lower().split()
        if keywords:
            kw_matched = sum(1 for kw in keywords if kw in text_blob)
            score += int((kw_matched / max(len(keywords), 1)) * 15)

        return min(score, 100)

    # ──────────────────────────────────────────────────
    # EXTRACTION HELPERS
    # ──────────────────────────────────────────────────
    def _extract_jobs_from_markdown(self, text, base_url, links):
        """Try to extract multiple job listings from markdown content."""
        jobs = []
        # Look for patterns like headers followed by details
        # Common pattern: ## Job Title  \n Company | Location
        sections = re.split(r'(?=^#{1,3}\s)', text, flags=re.MULTILINE)

        for section in sections:
            if len(section.strip()) < 30:
                continue
            lines = section.strip().split('\n')
            title_line = lines[0].strip().lstrip('#').strip()
            if not title_line or len(title_line) < 5:
                continue

            body = '\n'.join(lines[1:])
            job = {
                "id": self._generate_id(title_line, base_url),
                "title": self._clean_title(title_line),
                "company": self._extract_company(title_line, body, ""),
                "location": self._extract_location(body, ""),
                "type": self._extract_job_type(title_line, body),
                "seniority": self._extract_seniority(title_line, body),
                "snippet": body[:300],
                "apply_url": self._find_apply_link(body, links, base_url),
                "source": f"Fetched ({self._extract_domain(base_url)})",
                "posted_date": self._extract_date(body),
                "relevance_score": 0,
                "scraped_at": datetime.now().isoformat(),
            }
            jobs.append(job)

        return jobs if len(jobs) > 1 else []  # Only return if we found multiple

    @staticmethod
    def _generate_id(title, url):
        raw = f"{title}|{url}".lower().strip()
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    @staticmethod
    def _clean_title(title):
        """Clean up job title text."""
        # Remove common prefixes/suffixes
        title = re.sub(r'\s*[-|]\s*(LinkedIn|Indeed|Glassdoor|Naukri|Wellfound).*$', '', title, flags=re.IGNORECASE)
        title = re.sub(r'^(Job:|Position:|Hiring:)\s*', '', title, flags=re.IGNORECASE)
        return title.strip()

    @staticmethod
    def _extract_company(title, text, source):
        """Try to extract company name."""
        # From title patterns like "Role at Company" or "Role - Company"
        patterns = [
            r'(?:at|@)\s+([A-Z][\w\s&.]+?)(?:\s*[-|,]|$)',
            r'[-|]\s*([A-Z][\w\s&.]+?)(?:\s*[-|,]|$)',
            r'[Cc]ompany[:\s]+([^\n,]+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, title + " " + text[:200])
            if match:
                company = match.group(1).strip()
                if len(company) > 2 and len(company) < 60:
                    return company
        return ""

    @staticmethod
    def _extract_location(text, default=""):
        """Extract location from text."""
        patterns = [
            r'[Ll]ocation[:\s]+([^\n,;]+)',
            r'(Remote|Hybrid|On-?site)',
            r'(Bangalore|Bengaluru|Mumbai|Delhi|Hyderabad|Pune|Chennai|Noida|Gurgaon|Gurugram|Kolkata)',
            r'(New York|San Francisco|Seattle|London|Berlin|Singapore|Toronto|Austin)',
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return default

    @staticmethod
    def _extract_job_type(title, text):
        """Extract job type."""
        combined = f"{title} {text}".lower()
        if any(w in combined for w in ["intern", "internship"]):
            return "Internship"
        if any(w in combined for w in ["part-time", "part time"]):
            return "Part-time"
        if any(w in combined for w in ["contract", "freelance"]):
            return "Contract"
        if any(w in combined for w in ["full-time", "full time", "permanent"]):
            return "Full-time"
        return "Full-time"

    @staticmethod
    def _extract_seniority(title, text):
        """Extract seniority level."""
        combined = f"{title} {text}".lower()
        if any(w in combined for w in ["intern", "internship", "trainee"]):
            return "Intern"
        if any(w in combined for w in ["entry", "junior", "fresher", "new grad", "graduate"]):
            return "Entry"
        if any(w in combined for w in ["senior", "sr.", "lead", "principal", "staff"]):
            return "Senior"
        if any(w in combined for w in ["manager", "director", "vp", "head of"]):
            return "Leadership"
        return "Mid"

    @staticmethod
    def _extract_date(text):
        """Extract posting date from text."""
        patterns = [
            r'(\d{1,2}\s+(?:hours?|days?|weeks?)\s+ago)',
            r'[Pp]osted[:\s]+(\d{1,2}/\d{1,2}/\d{2,4})',
            r'(\d{4}-\d{2}-\d{2})',
            r'([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})',
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1)
        return ""

    @staticmethod
    def _find_apply_link(text, links, fallback_url):
        """Find the apply link from text or link list."""
        # Check extracted links
        if isinstance(links, list):
            for link in links:
                href = link if isinstance(link, str) else link.get("href", link.get("url", ""))
                if "apply" in href.lower():
                    return href
        # Check markdown links in text
        md_links = re.findall(r'\[([^\]]*(?:apply|job)[^\]]*)\]\(([^)]+)\)', text, re.IGNORECASE)
        if md_links:
            return md_links[0][1]
        return fallback_url

    @staticmethod
    def _is_noise_url(url, title):
        """Filter out non-job URLs."""
        noise_patterns = [
            r'salary', r'review', r'interview', r'company-overview',
            r'blog', r'article', r'news', r'/about', r'wikipedia',
        ]
        combined = f"{url} {title}".lower()
        return any(re.search(p, combined) for p in noise_patterns)

    @staticmethod
    def _portal_display_name(portal):
        """Get display name for a portal domain."""
        names = {
            "linkedin.com/jobs": "LinkedIn",
            "indeed.com": "Indeed",
            "glassdoor.com": "Glassdoor",
            "wellfound.com": "Wellfound",
            "naukri.com": "Naukri",
            "internshala.com": "Internshala",
            "web": "Web Search",
        }
        for key, name in names.items():
            if key in portal:
                return name
        return portal

    @staticmethod
    def _extract_domain(url):
        """Extract domain from URL."""
        match = re.search(r'https?://(?:www\.)?([^/]+)', url)
        return match.group(1) if match else url
