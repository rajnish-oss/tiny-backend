import io
import json
import logging
import os
import sys
import time
from typing import List, Literal, Optional

from pydantic import BaseModel, Field
from pypdf import PdfReader
from groq import Groq

MODEL = "openai/gpt-oss-120b"
logger = logging.getLogger("tinyfish.resume")


# 1. Schema = the fields of your search form
class JobFormData(BaseModel):
    full_name: Optional[str] = None
    role: str = Field(description="Best-fit job title to search for, e.g. 'Frontend Developer'")
    alternate_roles: List[str] = Field(default_factory=list, description="Up to 3 other suitable titles")
    location: Optional[str] = Field(None, description="Current city, or preferred city if stated")
    open_to_remote: bool = False
    seniority: Literal["Intern", "Entry", "Mid", "Senior", "Lead"]
    years_experience: Optional[float] = None
    keywords: List[str] = Field(description="Top 8-12 skills and tools, e.g. React, SQL, SEO")
    sources: List[str] = Field(
        default_factory=lambda: ["LinkedIn", "Indeed", "Naukri", "Internshala", "Company sites"],
        description="Job sources to search. Use Internshala only for intern/entry roles.",
    )


PROMPT = """You are a resume parser for a job search app.
Read the resume and fill the form fields.

Rules:
- Use only information present in the resume. If something is missing, use null or an empty list.
- seniority: Intern (students / internships only), Entry (0-2 yrs), Mid (3-5 yrs), Senior (6-9 yrs), Lead (10+ yrs or team lead).
- role: the most recent or most fitting job title, normalised (e.g. 'SDE-1' becomes 'Software Engineer').
- keywords: concrete skills and tools only, no soft skills, no duplicates.
- Estimate years_experience from employment dates; today's date is provided below.
"""


# 2. PDF to text
def pdf_to_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    logger.info("Resume step 1/4: extracted PDF text | pages=%s characters=%s", len(reader.pages), len(text))
    return text


# 3. Groq extraction
def extract_form_data_from_bytes(pdf_bytes: bytes) -> JobFormData:
    """Extract job-search preferences from PDF bytes without storing the resume."""
    from datetime import date

    started_at = time.perf_counter()
    logger.info("Resume parsing started | bytes=%s", len(pdf_bytes))
    logger.info("Resume step 2/4: creating Groq client")
    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    text = pdf_to_text(pdf_bytes)
    instructions = (
        f"{PROMPT}\nToday's date: {date.today().isoformat()}\n"
        "Return only a JSON object that validates against this schema:\n"
        f"{json.dumps(JobFormData.model_json_schema())}"
    )

    logger.info("Resume step 3/4: streaming structured extraction request to Groq")
    completion = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": instructions},
            {"role": "user", "content": f"RESUME TEXT:\n{text}"},
        ],
        temperature=1,
        max_completion_tokens=2048,
        top_p=1,
        reasoning_effort="medium",
        stream=True,
        stop=None,
    )
    output_text = "".join(
        chunk.choices[0].delta.content or ""
        for chunk in completion
        if chunk.choices
    )
    logger.info("Resume step 4/4: validating Groq response")
    form_data = JobFormData.model_validate_json(output_text)
    logger.info("Resume parsing complete | skills=%s duration=%.2fs", len(form_data.keywords), time.perf_counter() - started_at)
    return form_data


def extract_form_data(pdf_path: str) -> JobFormData:
    """Extract job-search preferences from a local PDF file."""
    with open(pdf_path, "rb") as resume_file:
        return extract_form_data_from_bytes(resume_file.read())


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python resume_parser.py resume.pdf")
    data = extract_form_data(sys.argv[1])
    print(json.dumps(data.model_dump(), indent=2))
